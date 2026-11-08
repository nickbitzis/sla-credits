"""Claim intake, validation, approval workflow, and disputes.

Claim states:
    rejected_invalid  failed validation (late, uncovered component, proactive contract, duplicate)
    rejected          SLA was met for the claimed components, or a reviewer rejected it
    disputed          the customer claims more downtime than measured; a dispute is open
    pending_review    valid, credit owed, waiting for support
    pending_finance   credit at or above the finance threshold, waiting for a second approver in finance
    approved          credit memo will be issued
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from slacredits.credits import credit_amount, to_usd_cents
from slacredits.engine import Engine
from slacredits.model import MONTHS, iso, month_bounds, month_end_date
from slacredits.probes import regions_of
from slacredits.store import Store
from slacredits.uptime import segments

FINANCE_THRESHOLD_USD_CENTS = 150_000  # $1,500
DISPUTE_TOLERANCE_MIN = 5
OPEN = ("pending_review", "pending_finance", "disputed")


class ClaimError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _row(r: dict) -> dict:
    r = dict(r)
    r["components"] = json.loads(r["components"])
    return r


def get_claim(store: Store, claim_id: str) -> dict:
    rows = store.q("SELECT * FROM claims WHERE id = ?", (claim_id,))
    if not rows:
        raise ClaimError("not_found", f"claim {claim_id} not found", 404)
    out = _row(rows[0])
    out["approvals"] = store.q("SELECT approver, role, on_date FROM approvals WHERE claim_id = ? ORDER BY on_date, approver", (claim_id,))
    d = store.q("SELECT id, status, decision FROM disputes WHERE claim_id = ?", (claim_id,))
    out["dispute"] = d[0] if d else None
    return out


def list_claims(store: Store, status: str | None = None, customer_id: str | None = None, limit: int = 500) -> list[dict]:
    sql, args = "SELECT * FROM claims WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if customer_id:
        sql += " AND customer_id = ?"
        args.append(customer_id)
    sql += " ORDER BY submitted_on DESC, id DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in store.q(sql, tuple(args))]


def deadline(engine: Engine, customer_id: str, month: str) -> date:
    c = engine.customer(customer_id)
    segs = segments(c, month)
    window = (segs[-1].version if segs else c.versions[-1]).claim_window_days
    return month_end_date(month) + timedelta(days=window)


def validate(engine: Engine, store: Store, customer_id: str, month: str, components: list[str], submitted_on: date) -> tuple[str | None, list[str]]:
    """Returns (error code or None, components that remain covered)."""
    if customer_id not in engine.ds.customers:
        raise ClaimError("unknown_customer", f"customer {customer_id} not found", 404)
    if month not in MONTHS:
        raise ClaimError("unknown_month", f"no monitoring data for {month}", 422)
    c = engine.customer(customer_id)
    segs = segments(c, month)
    if not segs:
        return "no_contract", []
    if submitted_on <= month_end_date(month):
        return "month_not_closed", []
    if segs[-1].version.proactive:
        return "proactive_contract", []
    if submitted_on > deadline(engine, customer_id, month):
        return "outside_claim_window", []
    covered = {x for s in segs for x in s.version.components}
    keep = [x for x in components if x in covered]
    if not keep:
        return "component_not_covered", []
    dup = store.q("SELECT id FROM claims WHERE customer_id = ? AND month = ? AND status NOT IN ('rejected_invalid')",
                  (customer_id, month))
    if dup:
        return "duplicate", []
    return None, keep


REASONS = {
    "no_contract": "No active contract in that month.",
    "month_not_closed": "Claims open the day after the month ends.",
    "proactive_contract": "This contract credits proactively; no claim is needed.",
    "outside_claim_window": "Submitted after the contract's claim window closed.",
    "component_not_covered": "None of the claimed components are covered by the contract.",
    "duplicate": "A claim for this customer and month already exists.",
    "sla_met": "Measured uptime met the SLA for the claimed components.",
}


def submit(engine: Engine, store: Store, customer_id: str, month: str, components: list[str], claimed_minutes: int,
           submitted_on: date, actor: str = "customer", ts: str | None = None) -> dict:
    err, keep = validate(engine, store, customer_id, month, components, submitted_on)
    c = engine.customer(customer_id)
    with store.tx():
        cid = store.next_id("CLM", "claims")
        if err:
            store.x("INSERT INTO claims (id, customer_id, month, components, claimed_minutes, submitted_on, status, reason, currency, "
                    "decided_on) VALUES (?, ?, ?, ?, ?, ?, 'rejected_invalid', ?, ?, ?)",
                    (cid, customer_id, month, json.dumps(components), claimed_minutes, submitted_on.isoformat(), err,
                     c.currency, submitted_on.isoformat()))
            store.audit(actor, "claim.rejected_invalid", cid, ts=ts, reason=err, customer_id=customer_id, month=month)
            return get_claim(store, cid)
        mc = engine.month(customer_id, month, components=keep)
        measured = mc["downtime_minutes"]
        status, reason, decided = "pending_review", None, None
        if claimed_minutes > measured + DISPUTE_TOLERANCE_MIN:
            status = "disputed"
        elif mc["credit"] == 0:
            status, reason, decided = "rejected", "sla_met", submitted_on.isoformat()
        store.x("INSERT INTO claims (id, customer_id, month, components, claimed_minutes, submitted_on, status, reason, "
                "measured_minutes, credit, currency, credit_usd_cents, decided_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, customer_id, month, json.dumps(keep), claimed_minutes, submitted_on.isoformat(), status, reason, measured,
                 mc["credit"], c.currency, mc["credit_usd_cents"], decided))
        store.audit(actor, "claim.submitted", cid, ts=ts, customer_id=customer_id, month=month, components=keep,
                    dropped=[x for x in components if x not in keep], claimed_minutes=claimed_minutes, measured_minutes=measured,
                    credit=mc["credit"], status=status)
        if status == "disputed":
            did = store.next_id("DSP", "disputes")
            store.x("INSERT INTO disputes (id, claim_id, opened_on, status, claimed_minutes, measured_minutes) VALUES (?, ?, ?, 'open', ?, ?)",
                    (did, cid, submitted_on.isoformat(), claimed_minutes, measured))
            store.audit("system", "dispute.opened", did, ts=ts, claim_id=cid, claimed_minutes=claimed_minutes, measured_minutes=measured)
    return get_claim(store, cid)


def approve(store: Store, claim_id: str, approver: str, role: str, on: date, ts: str | None = None) -> dict:
    cl = get_claim(store, claim_id)
    if cl["status"] not in ("pending_review", "pending_finance"):
        raise ClaimError("bad_state", f"claim is {cl['status']}, not awaiting approval", 409)
    if any(a["approver"] == approver for a in cl["approvals"]):
        raise ClaimError("same_approver", "the second approval must come from a different person", 409)
    if cl["status"] == "pending_finance" and role != "finance":
        raise ClaimError("finance_required", "credits of $1,500 or more need a finance approver", 403)
    with store.tx():
        store.x("INSERT INTO approvals (claim_id, approver, role, on_date) VALUES (?, ?, ?, ?)", (claim_id, approver, role, on.isoformat()))
        if cl["status"] == "pending_review" and cl["credit_usd_cents"] >= FINANCE_THRESHOLD_USD_CENTS:
            new = "pending_finance"
            store.x("UPDATE claims SET status = ? WHERE id = ?", (new, claim_id))
        else:
            new = "approved"
            store.x("UPDATE claims SET status = ?, decided_on = ? WHERE id = ?", (new, on.isoformat(), claim_id))
        store.audit(approver, "claim.approved" if new == "approved" else "claim.first_approval", claim_id, ts=ts, role=role,
                    credit=cl["credit"], currency=cl["currency"], status=new)
    return get_claim(store, claim_id)


def reject(store: Store, claim_id: str, approver: str, reason: str, on: date, ts: str | None = None) -> dict:
    cl = get_claim(store, claim_id)
    if cl["status"] not in ("pending_review", "pending_finance"):
        raise ClaimError("bad_state", f"claim is {cl['status']}, not awaiting approval", 409)
    with store.tx():
        store.x("UPDATE claims SET status = 'rejected', reason = ?, decided_on = ? WHERE id = ?", (reason, on.isoformat(), claim_id))
        store.audit(approver, "claim.rejected", claim_id, ts=ts, reason=reason)
    return get_claim(store, claim_id)


# ---------------------------------------------------------------- disputes

def get_dispute(store: Store, dispute_id: str) -> dict:
    rows = store.q("SELECT * FROM disputes WHERE id = ?", (dispute_id,))
    if not rows:
        raise ClaimError("not_found", f"dispute {dispute_id} not found", 404)
    return rows[0]


def list_disputes(store: Store, status: str | None = None) -> list[dict]:
    if status:
        return store.q("SELECT * FROM disputes WHERE status = ? ORDER BY opened_on DESC", (status,))
    return store.q("SELECT * FROM disputes ORDER BY opened_on DESC")


def evidence_pack(engine: Engine, store: Store, dispute_id: str) -> dict:
    """Everything needed to explain the measured number: terms, probe timeline, incidents, exclusions, math."""
    d = get_dispute(store, dispute_id)
    cl = get_claim(store, d["claim_id"])
    cid, month, comps = cl["customer_id"], cl["month"], cl["components"]
    c = engine.customer(cid)
    mc = engine.month(cid, month, components=comps)
    lo, hi = month_bounds(month)
    timeline = []
    exclusions = []
    for sg in segments(c, month):
        v = sg.version
        for comp in comps:
            if comp not in v.components:
                continue
            excl = engine.exclusions(v, cid, comp, sg.start, sg.end)
            exclusions += [{"component": comp, "version": v.version, **e.to_dict()} for e in excl]
            applied = [(e.start, e.end, e.ref) for e in excl if e.applied]
            for s, e, mask in engine.ds.probes.timeline(comp, sg.start, sg.end):
                n = bin(mask).count("1")
                ex = next((ref for a, b, ref in applied if a < e and b > s), None)
                timeline.append({"component": comp, "start": iso(s), "end": iso(e), "minutes": e - s,
                                 "regions_failing": regions_of(mask), "meets_quorum": n >= v.quorum,
                                 "excluded_by": ex})
    timeline.sort(key=lambda r: (r["start"], r["component"]))
    incidents = [i.public() for i in engine.ds.incidents
                 if i.start < hi and i.end > lo and set(i.components) & set(comps)]
    return {
        "dispute": d, "claim": cl,
        "customer": c.summary(),
        "contract_terms": [s.version.terms() for s in segments(c, month)],
        "measured": mc,
        "difference_minutes": d["claimed_minutes"] - d["measured_minutes"],
        "probe_timeline": timeline,
        "probe_timeline_note": "Runs of minutes where at least one region's probe failed. Minutes not listed passed in all 3 regions.",
        "incidents": incidents,
        "exclusions": exclusions,
        "data_fingerprint": engine.ds.data_fingerprint(),
        "audit": store.audit_log(entity=d["claim_id"]) + store.audit_log(entity=dispute_id),
    }


def resolve(engine: Engine, store: Store, dispute_id: str, decision: str, resolver: str, notes: str, on: date,
            goodwill_pct: int = 0, ts: str | None = None) -> dict:
    """upheld: credit at the measured figure. goodwill: measured credit plus a goodwill percent of the fee, within the cap.
    accepted: recompute with the customer's downtime figure."""
    d = get_dispute(store, dispute_id)
    if d["status"] != "open":
        raise ClaimError("bad_state", "dispute already resolved", 409)
    if decision not in ("upheld", "goodwill", "accepted"):
        raise ClaimError("bad_decision", "decision must be upheld, goodwill, or accepted", 422)
    cl = get_claim(store, d["claim_id"])
    cid, month = cl["customer_id"], cl["month"]
    if decision == "accepted":
        mc = engine.month(cid, month, components=cl["components"], claimed_minutes=d["claimed_minutes"])
        final = mc["credit"]
    else:
        mc = engine.month(cid, month, components=cl["components"])
        final = mc["credit"]
        if decision == "goodwill":
            if not 0 < goodwill_pct <= 20:
                raise ClaimError("bad_goodwill", "goodwill_pct must be 1 to 20", 422)
            last = segments(engine.customer(cid), month)[-1].version
            cap = credit_amount(mc["fee_total"], last.cap_pct)
            final = min(cap, final + credit_amount(mc["fee_total"], goodwill_pct))
    fx = engine.ds.fx_to_usd[month]
    usd = to_usd_cents(final, cl["currency"], fx)
    status = "approved" if final > 0 else "rejected"
    with store.tx():
        store.x("UPDATE disputes SET status = 'resolved', decision = ?, goodwill_pct = ?, final_credit = ?, resolved_by = ?, "
                "resolved_on = ?, notes = ? WHERE id = ?",
                (decision, goodwill_pct or None, final, resolver, on.isoformat(), notes, dispute_id))
        store.x("UPDATE claims SET status = ?, credit = ?, credit_usd_cents = ?, decided_on = ?, reason = ? WHERE id = ?",
                (status, final, usd, on.isoformat(), f"dispute {decision}", cl["id"]))
        store.audit(resolver, "dispute.resolved", dispute_id, ts=ts, decision=decision, goodwill_pct=goodwill_pct,
                    final_credit=final, measured_credit=mc["credit"] if decision != "accepted" else None, notes=notes)
    return {"dispute": get_dispute(store, dispute_id), "claim": get_claim(store, cl["id"])}

