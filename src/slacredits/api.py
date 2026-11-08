"""HTTP API and web UI.

Errors share one shape: {"error": code, "message": ...}. Workflow actions
take the acting user from the request body and are written to the audit log.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from importlib import resources

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from slacredits import claims, finance, metrics, status
from slacredits.engine import Engine
from slacredits.logs import log, request_id, setup_logging
from slacredits.model import COMPONENTS, MONTHS
from slacredits.seed import seed_history
from slacredits.store import Store
from slacredits.uptime import segments

logger = logging.getLogger("slacredits.api")


class ClaimIn(BaseModel):
    customer_id: str
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    components: list[str] = Field(min_length=1, max_length=3)
    claimed_minutes: int = Field(ge=0, le=44640)
    submitted_on: date | None = None


class ApproveIn(BaseModel):
    approver: str = Field(min_length=2, max_length=60)
    role: str = Field(pattern="^(support|finance)$")
    on: date | None = None


class RejectIn(BaseModel):
    approver: str = Field(min_length=2, max_length=60)
    reason: str = Field(min_length=3, max_length=300)
    on: date | None = None


class ResolveIn(BaseModel):
    decision: str = Field(pattern="^(upheld|goodwill|accepted)$")
    resolver: str = Field(min_length=2, max_length=60)
    notes: str = Field(min_length=3, max_length=1000)
    goodwill_pct: int = Field(default=0, ge=0, le=20)
    on: date | None = None


def create_app(engine: Engine, store: Store, seed_if_empty: bool = True) -> FastAPI:
    setup_logging()
    if seed_if_empty and not store.get_meta("seeded"):
        t0 = time.perf_counter()
        info = seed_history(engine, store)
        log(logger, "seeded claim history", claims=info["claims"], seconds=round(time.perf_counter() - t0, 2))

    app = FastAPI(title="slacredits", version="1.0.0", description="SLA and service credit engine for Corvane (synthetic).")

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id.set(rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        dt = time.perf_counter() - t0
        metrics.inc("slacredits_http_requests_total", route=path, status=str(response.status_code))
        metrics.observe("slacredits_http_request_seconds", dt, route=path)
        response.headers["x-request-id"] = rid
        if path not in ("/healthz", "/metrics"):
            log(logger, "request", method=request.method, route=path, status=response.status_code, ms=round(dt * 1000, 1), request_id=rid)
        return response

    @app.exception_handler(claims.ClaimError)
    async def claim_error(_: Request, e: claims.ClaimError):
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)

    @app.exception_handler(KeyError)
    async def not_found(_: Request, e: KeyError):
        return JSONResponse({"error": "not_found", "message": f"unknown id {e.args[0] if e.args else ''}"}, status_code=404)

    def _month(month: str) -> str:
        if month not in MONTHS:
            raise claims.ClaimError("unknown_month", f"month must be one of {MONTHS[0]} to {MONTHS[-1]}", 422)
        return month

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return resources.files("slacredits").joinpath("static/index.html").read_text()

    @app.get("/healthz")
    def healthz() -> dict:
        store.q("SELECT 1")
        return {"status": "ok", "as_of": engine.as_of.isoformat(), "customers": len(engine.ds.customers),
                "data_fingerprint": engine.ds.data_fingerprint()}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom() -> str:
        gauges = [("slacredits_claims", "Claims by status.", {"status": r["status"]}, r["n"])
                  for r in store.q("SELECT status, COUNT(*) n FROM claims GROUP BY status")]
        gauges.append(("slacredits_disputes_open", "Disputes waiting for resolution.", {},
                       store.q("SELECT COUNT(*) n FROM disputes WHERE status = 'open'")[0]["n"]))
        gauges.append(("slacredits_accrual_usd_cents", "Expected credits not yet issued, in US cents.", {},
                       finance.accrual(engine, store)["total_usd_cents"]))
        gauges.append(("slacredits_customers", "Customers under contract.", {}, len(engine.ds.customers)))
        return metrics.render(gauges)

    @app.get("/v1/meta")
    def meta() -> dict:
        return {"vendor": "Corvane", "months": MONTHS, "components": list(COMPONENTS), "as_of": engine.as_of.isoformat(),
                "seed": engine.ds.seed, "data_fingerprint": engine.ds.data_fingerprint()}

    @app.get("/v1/customers")
    def customers(segment: str | None = None) -> dict:
        rows = [c.summary() for c in engine.ds.customers.values() if segment is None or c.segment == segment]
        return {"customers": rows, "count": len(rows)}

    @app.get("/v1/customers/{cid}")
    def customer(cid: str) -> dict:
        c = engine.customer(cid)
        return {**c.summary(), "contract_versions": [v.terms() for v in c.versions]}

    @app.get("/v1/customers/{cid}/uptime")
    def uptime_year(cid: str) -> dict:
        c = engine.customer(cid)
        rows = []
        for mc in engine.year(cid):
            seg = mc["segments"][-1] if mc["segments"] else None
            rows.append({"month": mc["month"], "active": mc["active"], "uptime_pct": mc["uptime_pct"],
                         "target": seg["target"] if seg else None, "downtime_minutes": mc["downtime_minutes"],
                         "credit": mc["credit"], "segments": len(mc["segments"]),
                         "components": {u["component"]: u["uptime_pct"] for s in mc["segments"] for u in s["components"]}})
        return {"customer_id": cid, "currency": c.currency, "months": rows}

    @app.get("/v1/customers/{cid}/uptime/{month}")
    def uptime_month(cid: str, month: str) -> dict:
        _month(month)
        c = engine.customer(cid)
        mc = engine.month(cid, month)
        excl = []
        for sg in segments(c, month):
            for comp in sg.version.components:
                excl += [{"component": comp, "version": sg.version.version, **e.to_dict()}
                         for e in engine.exclusions(sg.version, cid, comp, sg.start, sg.end)]
        return {**mc, "exclusions": excl}

    @app.get("/v1/customers/{cid}/credits")
    def customer_credits(cid: str) -> dict:
        c = engine.customer(cid)
        cl = {r["month"]: r for r in claims.list_claims(store, customer_id=cid)[::-1]}
        rows = []
        for mc in engine.year(cid):
            m = mc["month"]
            claim = cl.get(m)
            if mc["basis"] == "proactive":
                state = "issued" if mc["credit"] > 0 else "none_owed"
            elif claim:
                state = f"claim {claim['status']}"
            elif mc["credit"] == 0:
                state = "none_owed"
            else:
                state = "unclaimed: window open" if engine.as_of <= claims.deadline(engine, cid, m) else "unclaimed: window closed"
            rows.append({"month": m, "basis": mc["basis"], "uptime_pct": mc["uptime_pct"], "fee": mc["fee_total"],
                         "credit_computed": mc["credit"], "claim_id": claim["id"] if claim else None,
                         "credit_issued": (mc["credit"] if mc["basis"] == "proactive" else
                                           (claim["credit"] if claim and claim["status"] == "approved" else 0)),
                         "state": state})
        return {"customer_id": cid, "currency": c.currency, "months": rows,
                "total_issued": sum(r["credit_issued"] for r in rows), "total_computed": sum(r["credit_computed"] for r in rows)}

    @app.get("/v1/credits")
    def credits_month(month: str = Query(...)) -> dict:
        _month(month)
        rows = [{"customer_id": r["customer_id"], "segment": r["segment"], "basis": r["basis"], "currency": r["currency"],
                 "uptime_pct": r["uptime_pct"], "credit": r["credit"], "credit_usd_cents": r["credit_usd_cents"]}
                for r in engine.all_month(month) if r["active"]]
        return {"month": month, "customers": rows, "credited": sum(1 for r in rows if r["credit"] > 0),
                "total_usd_cents": sum(r["credit_usd_cents"] for r in rows)}

    @app.get("/v1/compare")
    def compare(month: str = Query(...), component: str = Query("api")) -> dict:
        _month(month)
        if component not in COMPONENTS:
            raise claims.ClaimError("unknown_component", f"component must be one of {', '.join(COMPONENTS)}", 422)
        return {"month": month, "component": component, "definitions": engine.compare(month, component)}

    @app.get("/v1/claims")
    def list_claims(status: str | None = None, customer_id: str | None = None, limit: int = Query(200, le=2000)) -> dict:
        rows = claims.list_claims(store, status, customer_id, limit)
        counts = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) n FROM claims GROUP BY status")}
        return {"claims": rows, "counts": counts}

    @app.get("/v1/claims/{claim_id}")
    def get_claim(claim_id: str) -> dict:
        return claims.get_claim(store, claim_id)

    @app.post("/v1/claims", status_code=201)
    def submit(body: ClaimIn) -> dict:
        bad = [x for x in body.components if x not in COMPONENTS]
        if bad:
            raise claims.ClaimError("unknown_component", f"unknown component {bad[0]}", 422)
        cl = claims.submit(engine, store, body.customer_id, body.month, body.components, body.claimed_minutes,
                           body.submitted_on or engine.as_of, actor=f"customer:{body.customer_id}")
        metrics.inc("slacredits_claims_submitted_total", status=cl["status"])
        return cl

    @app.post("/v1/claims/{claim_id}/approve")
    def approve(claim_id: str, body: ApproveIn) -> dict:
        cl = claims.approve(store, claim_id, body.approver, body.role, body.on or engine.as_of)
        metrics.inc("slacredits_claim_decisions_total", action="approve")
        return cl

    @app.post("/v1/claims/{claim_id}/reject")
    def reject(claim_id: str, body: RejectIn) -> dict:
        cl = claims.reject(store, claim_id, body.approver, body.reason, body.on or engine.as_of)
        metrics.inc("slacredits_claim_decisions_total", action="reject")
        return cl

    @app.get("/v1/disputes")
    def disputes(status: str | None = None) -> dict:
        return {"disputes": claims.list_disputes(store, status)}

    @app.get("/v1/disputes/{dispute_id}/evidence")
    def evidence(dispute_id: str) -> dict:
        return claims.evidence_pack(engine, store, dispute_id)

    @app.post("/v1/disputes/{dispute_id}/resolve")
    def resolve(dispute_id: str, body: ResolveIn) -> dict:
        out = claims.resolve(engine, store, dispute_id, body.decision, body.resolver, body.notes, body.on or engine.as_of,
                             goodwill_pct=body.goodwill_pct)
        metrics.inc("slacredits_disputes_resolved_total", decision=body.decision)
        return out

    @app.get("/v1/finance/memos")
    def memos(month: str = Query(...), format: str = Query("json", pattern="^(json|csv)$"), actor: str = "api"):
        _month(month)
        store.audit(actor, "finance.export", f"memos:{month}", format=format)
        if format == "csv":
            return Response(finance.memos_csv(engine, store, month), media_type="text/csv",
                            headers={"content-disposition": f'attachment; filename="credit-memos-{month}.csv"'})
        return finance.memos_json(engine, store, month)

    @app.get("/v1/finance/accrual")
    def accrual(as_of: date | None = None) -> dict:
        return finance.accrual(engine, store, as_of)

    @app.get("/v1/finance/revenue-impact")
    def revenue_impact() -> dict:
        return finance.revenue_impact(engine, store)

    @app.get("/v1/incidents")
    def incidents() -> dict:
        return {"incidents": [i.public() for i in engine.ds.incidents]}

    @app.get("/v1/status")
    def status_summary() -> dict:
        return status.summary(engine.ds)

    @app.get("/v1/status/history")
    def status_history(component: str = Query("api"), days: int = Query(90, ge=1, le=365)) -> dict:
        if component not in COMPONENTS:
            raise claims.ClaimError("unknown_component", f"component must be one of {', '.join(COMPONENTS)}", 422)
        return {"component": component, "days": status.history(engine.ds, component)[-days:]}

    @app.get("/v1/status/heatmap")
    def heatmap() -> dict:
        return {"components": {c: [{"date": d["date"], "status": d["status"], "uptime_pct": d["uptime_pct"],
                                    "down_minutes": d["down_minutes"], "incidents": d["incidents"]}
                                   for d in status.history(engine.ds, c)] for c in COMPONENTS}}

    @app.get("/v1/audit")
    def audit(entity: str | None = None, limit: int = Query(100, le=1000)) -> dict:
        return {"entries": store.audit_log(entity, limit)}

    return app
