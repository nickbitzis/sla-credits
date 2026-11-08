from __future__ import annotations

from datetime import date, timedelta

from slacredits.model import month_end_date

MONTH = "2026-03"


def claim_customer(engine, min_usd=0, max_usd=10**12, month=MONTH, single=True):
    """A claim-required customer with a credit owed in `month`."""
    for cid in engine.ds.customers:
        mc = engine.month(cid, month)
        if (mc["basis"] == "claim_required" and mc["credit"] > 0 and min_usd <= mc["credit_usd_cents"] < max_usd
                and (not single or len(mc["segments"]) == 1)):
            return cid, mc
    raise LookupError("no customer")


def in_window(month=MONTH, days=5) -> date:
    return month_end_date(month) + timedelta(days=days)
