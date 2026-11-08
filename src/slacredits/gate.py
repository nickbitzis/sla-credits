"""Credit regression gate.

A golden file pins one sample month: the data fingerprint, each customer's
contract fingerprint, and each customer's credit. The gate recomputes the
month and fails when a customer's credit changed while neither its contract
nor the monitoring data changed. That catches code changes that move money
by accident. Changes explained by a contract or data change are reported and
allowed; regenerate the golden file to accept them.
"""

from __future__ import annotations

import json
from pathlib import Path

from slacredits.engine import Engine
from slacredits.synth import contract_fingerprint

GOLDEN_MONTH = "2026-03"


def snapshot(engine: Engine, month: str = GOLDEN_MONTH) -> dict:
    custs = {}
    for cid, c in engine.ds.customers.items():
        mc = engine.month(cid, month)
        custs[cid] = {"contract": contract_fingerprint(c), "credit": mc["credit"], "currency": c.currency,
                      "uptime_pct": mc["uptime_pct"]}
    return {"month": month, "seed": engine.ds.seed, "data_fingerprint": engine.ds.data_fingerprint(), "customers": custs,
            "total_credit_usd_cents": sum(engine.month(cid, month)["credit_usd_cents"] for cid in custs)}


def write_golden(engine: Engine, path: str | Path, month: str = GOLDEN_MONTH) -> dict:
    snap = snapshot(engine, month)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    return snap


def check(engine: Engine, golden: dict) -> dict:
    now = snapshot(engine, golden["month"])
    data_changed = now["data_fingerprint"] != golden["data_fingerprint"]
    violations, explained = [], []
    for cid in sorted(set(golden["customers"]) | set(now["customers"])):
        old, new = golden["customers"].get(cid), now["customers"].get(cid)
        if old is None or new is None:
            explained.append({"customer_id": cid, "why": "customer added" if old is None else "customer removed"})
            continue
        if old["credit"] == new["credit"]:
            continue
        row = {"customer_id": cid, "old_credit": old["credit"], "new_credit": new["credit"], "currency": new["currency"]}
        if old["contract"] != new["contract"]:
            explained.append({**row, "why": "contract changed"})
        elif data_changed:
            explained.append({**row, "why": "monitoring data changed"})
        else:
            violations.append({**row, "why": "credit changed with no contract or data change"})
    return {"month": golden["month"], "data_changed": data_changed, "checked": len(now["customers"]),
            "violations": violations, "explained": explained, "ok": not violations}
