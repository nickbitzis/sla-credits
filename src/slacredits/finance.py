"""Finance views: credit memos, accrual for credits not yet issued, and revenue impact by segment.

Credit memos are dated by service month (the month the SLA was missed).
Proactive credits are issued 3 days after the month ends. Claim credits are
issued when the claim is approved or its dispute resolves with a credit.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, timedelta

from slacredits.claims import OPEN, deadline
from slacredits.engine import Engine
from slacredits.model import MONTHS, SEGMENTS, month_end_date
from slacredits.store import Store

PROACTIVE_ISSUE_DAYS = 3
MEMO_FIELDS = ["memo_id", "customer_id", "customer_name", "segment", "service_month", "basis", "claim_id", "uptime_pct",
               "tier_pct", "currency", "amount", "fx_to_usd", "amount_usd", "issued_on"]


def _claims_by_cm(store: Store) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in store.q("SELECT * FROM claims"):
        out[(r["customer_id"], r["month"])].append(r)
    return out


def memos(engine: Engine, store: Store, month: str, as_of: date | None = None) -> list[dict]:
    as_of = as_of or engine.as_of
    rows = []
    issue = month_end_date(month) + timedelta(days=PROACTIVE_ISSUE_DAYS)
    for cid, c in engine.ds.customers.items():
        mc = engine.month(cid, month)
        if mc["basis"] == "proactive" and mc["credit"] > 0 and issue <= as_of:
            rows.append(_memo(c, mc, "proactive", None, mc["credit"], mc["credit_usd_cents"], issue))
    for cl in store.q("SELECT * FROM claims WHERE month = ? AND status = 'approved' AND credit > 0 AND decided_on <= ?",
                      (month, as_of.isoformat())):
        c = engine.customer(cl["customer_id"])
        mc = engine.month(cl["customer_id"], month)
        rows.append(_memo(c, mc, "claim", cl["id"], cl["credit"], cl["credit_usd_cents"], date.fromisoformat(cl["decided_on"])))
    rows.sort(key=lambda r: r["customer_id"])
    return rows


def _memo(c, mc: dict, basis: str, claim_id: str | None, amount: int, usd: int, issued: date) -> dict:
    tiers = [s["tier_pct"] for s in mc["segments"]]
    return {"memo_id": f"CM-{mc['month'].replace('-', '')}-{c.id}", "customer_id": c.id, "customer_name": c.name,
            "segment": c.segment, "service_month": mc["month"], "basis": basis, "claim_id": claim_id,
            "uptime_pct": mc["uptime_pct"], "tier_pct": max(tiers) if tiers else 0, "currency": c.currency,
            "amount": amount, "fx_to_usd": mc["fx_to_usd"], "amount_usd": usd, "issued_on": issued.isoformat()}


def memo_totals(rows: list[dict]) -> dict:
    by_cur: dict[str, int] = defaultdict(int)
    for r in rows:
        by_cur[r["currency"]] += r["amount"]
    return {"count": len(rows), "by_currency": dict(sorted(by_cur.items())), "usd_cents": sum(r["amount_usd"] for r in rows)}


def memos_json(engine: Engine, store: Store, month: str, as_of: date | None = None) -> dict:
    rows = memos(engine, store, month, as_of)
    return {"service_month": month, "as_of": (as_of or engine.as_of).isoformat(), "memos": rows, "totals": memo_totals(rows),
            "amount_units": "minor units of the memo currency; amount_usd in US cents"}


def memos_csv(engine: Engine, store: Store, month: str, as_of: date | None = None) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MEMO_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in memos(engine, store, month, as_of):
        w.writerow({**r, "amount": f"{r['amount'] / 100:.2f}", "amount_usd": f"{r['amount_usd'] / 100:.2f}",
                    "claim_id": r["claim_id"] or ""})
    return buf.getvalue()


def accrual(engine: Engine, store: Store, as_of: date | None = None) -> dict:
    """Credits likely to be issued after `as_of` for months already closed.

    proactive_pending      proactive credit not yet issued: 100%
    claim_open             claim pending review, pending finance, or disputed: measured credit at 100%
    unclaimed_window_open  credit owed, no claim yet, window still open: credit x historical claim rate of the segment
    """
    as_of = as_of or engine.as_of
    by_cm = _claims_by_cm(store)
    rows = []
    for month in MONTHS:
        if month_end_date(month) >= as_of:
            continue
        issue = month_end_date(month) + timedelta(days=PROACTIVE_ISSUE_DAYS)
        for cid in engine.ds.customers:
            mc = engine.month(cid, month)
            if mc["credit"] <= 0 and not by_cm.get((cid, month)):
                continue
            claims = [c for c in by_cm.get((cid, month), []) if c["status"] != "rejected_invalid"]
            kind, prob, base = None, 0.0, mc["credit_usd_cents"]
            if mc["basis"] == "proactive":
                if mc["credit"] > 0 and issue > as_of:
                    kind, prob = "proactive_pending", 1.0
            elif claims:
                open_ = [c for c in claims if c["status"] in OPEN and c["submitted_on"] <= as_of.isoformat()]
                if open_:
                    kind, prob, base = "claim_open", 1.0, open_[0]["credit_usd_cents"]
            elif mc["credit"] > 0 and as_of <= deadline(engine, cid, month):
                kind, prob = "unclaimed_window_open", engine.ds.claim_rate[mc["segment"]]
            if kind and base > 0:
                rows.append({"customer_id": cid, "segment": mc["segment"], "month": month, "kind": kind, "probability": prob,
                             "credit_usd_cents": base, "expected_usd_cents": round(base * prob)})
    by_kind: dict[str, int] = defaultdict(int)
    by_seg: dict[str, int] = defaultdict(int)
    for r in rows:
        by_kind[r["kind"]] += r["expected_usd_cents"]
        by_seg[r["segment"]] += r["expected_usd_cents"]
    return {"as_of": as_of.isoformat(), "claim_rate": engine.ds.claim_rate, "rows": rows,
            "by_kind": dict(by_kind), "by_segment": dict(by_seg), "total_usd_cents": sum(by_kind.values())}


def revenue_impact(engine: Engine, store: Store, as_of: date | None = None) -> dict:
    as_of = as_of or engine.as_of
    by_cm = _claims_by_cm(store)
    seg = {s: defaultdict(int) for s in SEGMENTS}
    months = []
    for month in MONTHS:
        issued = memos(engine, store, month, as_of)
        issued_usd = defaultdict(int)
        for r in issued:
            issued_usd[r["segment"]] += r["amount_usd"]
        m_fee = 0
        for cid in engine.ds.customers:
            mc = engine.month(cid, month)
            s = seg[mc["segment"]]
            s["fee_usd_cents"] += mc["fee_usd_cents"]
            m_fee += mc["fee_usd_cents"]
            if mc["credit"] > 0:
                s["customer_months_breached"] += 1
                s["computed_usd_cents"] += mc["credit_usd_cents"]
                claimed = [c for c in by_cm.get((cid, month), []) if c["status"] != "rejected_invalid"]
                if mc["basis"] == "claim_required" and not claimed and as_of > deadline(engine, cid, month):
                    s["forfeited_usd_cents"] += mc["credit_usd_cents"]
        for k, v in issued_usd.items():
            seg[k]["issued_usd_cents"] += v
        months.append({"month": month, "fee_usd_cents": m_fee, "issued_usd_cents": sum(issued_usd.values()),
                       "memos": len(issued)})
    acc = accrual(engine, store, as_of)
    out = []
    for s in SEGMENTS:
        d = seg[s]
        fee = d["fee_usd_cents"]
        out.append({"segment": s, "customers": sum(1 for c in engine.ds.customers.values() if c.segment == s),
                    "fee_usd_cents": fee, "issued_usd_cents": d["issued_usd_cents"],
                    "accrued_usd_cents": acc["by_segment"].get(s, 0), "computed_usd_cents": d["computed_usd_cents"],
                    "forfeited_usd_cents": d["forfeited_usd_cents"], "customer_months_breached": d["customer_months_breached"],
                    "issued_pct_of_fees": round(100 * d["issued_usd_cents"] / fee, 3) if fee else 0.0})
    total = {k: sum(r[k] for r in out) for k in ("fee_usd_cents", "issued_usd_cents", "accrued_usd_cents", "computed_usd_cents",
                                                    "forfeited_usd_cents", "customer_months_breached")}
    total["issued_pct_of_fees"] = round(100 * total["issued_usd_cents"] / total["fee_usd_cents"], 3)
    return {"as_of": as_of.isoformat(), "segments": out, "total": total, "months": months, "accrual_total_usd_cents": acc["total_usd_cents"]}
