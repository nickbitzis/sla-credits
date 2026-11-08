"""Seeded claim history, run through the same workflow functions the API uses.

Claim-required customers with a credit owed file a claim with their
segment's claim rate. 15% of those file late, 20% claim more downtime than
was measured (a dispute), and a few claim a component their contract does
not cover or file for a month where the SLA was met. Reviewers work the
queue with a delay, so the most recent claims are still open on the demo
date.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from slacredits import claims
from slacredits.engine import Engine
from slacredits.model import COMPONENTS, MONTHS, month_end_date
from slacredits.store import Store

SUPPORT = ["support.lead.a", "support.lead.b"]
FINANCE = "finance.controller"


def seed_history(engine: Engine, store: Store, seed: int | None = None) -> dict:
    rng = random.Random((seed if seed is not None else engine.ds.seed) + 1)
    as_of = engine.as_of
    filed = []
    for month in MONTHS:
        end = month_end_date(month)
        for cid, c in engine.ds.customers.items():
            mc = engine.month(cid, month)
            if not mc["active"] or mc["basis"] == "proactive":
                continue
            window = mc["claim_window_days"]
            covered = mc["covered"]
            if mc["credit"] > 0:
                if rng.random() >= engine.ds.claim_rate[c.segment]:
                    continue
                late = rng.random() < 0.15
                offset = rng.randint(window + 1, window + 20) if late else rng.randint(2, window)
                worst = [s["worst_component"] for s in mc["segments"] if s["worst_component"]]
                comps = sorted(set(worst))
                measured = engine.month(cid, month, components=comps)["downtime_minutes"]
                claimed = measured if rng.random() >= 0.2 else measured + rng.randint(20, 240)
            else:
                r = rng.random()
                if r < 0.03:  # SLA met, customer saw regional errors and claims anyway
                    comps, offset = [rng.choice(covered)], rng.randint(2, window)
                    measured = engine.month(cid, month, components=comps)["downtime_minutes"]
                    claimed = measured + rng.choice([0, 0, 30, 90])
                elif r < 0.045:  # claims a component the contract does not cover
                    other = [x for x in COMPONENTS if x not in covered]
                    if not other:
                        continue
                    comps, offset, claimed = [rng.choice(other)], rng.randint(2, window), rng.randint(15, 120)
                else:
                    continue
            on = end + timedelta(days=offset)
            if on > as_of:
                continue
            filed.append((on, cid, month, comps, claimed))
    filed.sort()
    for on, cid, month, comps, claimed in filed:
        claims.submit(engine, store, cid, month, comps, claimed, on, actor=f"customer:{cid}", ts=f"{on.isoformat()}T10:00:00Z")
    _work_queue(engine, store, rng, as_of)
    store.set_meta("seeded", "1")
    return {"claims": len(filed)}


def _work_queue(engine: Engine, store: Store, rng: random.Random, as_of: date) -> None:
    for cl in claims.list_claims(store, limit=100000)[::-1]:
        sub = date.fromisoformat(cl["submitted_on"])
        if cl["status"] == "pending_review":
            on = sub + timedelta(days=rng.randint(3, 16))
            if on > as_of:
                continue
            who = rng.choice(SUPPORT)
            cl = claims.approve(store, cl["id"], who, "support", on, ts=f"{on.isoformat()}T15:00:00Z")
            if cl["status"] == "pending_finance":
                on2 = on + timedelta(days=rng.randint(2, 10))
                if on2 <= as_of:
                    claims.approve(store, cl["id"], FINANCE, "finance", on2, ts=f"{on2.isoformat()}T16:00:00Z")
        elif cl["status"] == "disputed":
            on = sub + timedelta(days=rng.randint(10, 35))
            if on > as_of:
                continue
            d = store.q("SELECT id FROM disputes WHERE claim_id = ?", (cl["id"],))[0]
            r = rng.random()
            if r < 0.7:
                claims.resolve(engine, store, d["id"], "upheld", "sla.desk", "Probe evidence reviewed with the customer; measured figure stands.",
                               on, ts=f"{on.isoformat()}T12:00:00Z")
            elif r < 0.95:
                claims.resolve(engine, store, d["id"], "goodwill", "sla.desk", "Measured figure stands; goodwill credit for the support burden.",
                               on, goodwill_pct=5, ts=f"{on.isoformat()}T12:00:00Z")
            else:
                claims.resolve(engine, store, d["id"], "accepted", "sla.desk", "Customer logs show errors our probes missed; claim accepted.",
                               on, ts=f"{on.isoformat()}T12:00:00Z")
