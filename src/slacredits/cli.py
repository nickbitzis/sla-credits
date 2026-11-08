"""Command line: run the API, explain a customer month, compare definitions, finance exports, and the credit gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

from slacredits.credits import fmt_money
from slacredits.engine import Engine
from slacredits.model import MONTHS
from slacredits.store import Store
from slacredits.synth import DEFAULT_SEED, generate

DEFAULT_DB = os.environ.get("SLACREDITS_DB_PATH", "data/slacredits.db")
DEFAULT_GOLDEN = "golden/credits-2026-03.json"


def _engine(seed: int) -> Engine:
    as_of = os.environ.get("SLACREDITS_AS_OF")
    return Engine(generate(seed), date.fromisoformat(as_of) if as_of else None)


def _store(db: str, engine: Engine) -> Store:
    from slacredits.seed import seed_history

    store = Store(db)
    if not store.get_meta("seeded"):
        seed_history(engine, store)
    return store


def explain(engine: Engine, cid: str, month: str) -> str:
    c = engine.customer(cid)
    mc = engine.month(cid, month)
    lines = [f"{c.id} {c.name} ({c.segment}, {c.currency}), {month}, credit basis: {mc['basis']}"]
    for s in mc["segments"]:
        lines.append(f"  contract v{s['version']} from {s['from'][:10]}, {s['days']} of {mc['days_in_month']} days: "
                     f"target {s['target']}%, {s['definition']}")
        for u in s["components"]:
            ex = f", excluded {u['excluded_minutes']} min ({', '.join(u['exclusions_applied'])})" if u["exclusions_applied"] else ""
            lines.append(f"    {u['component']:<9} raw down {u['raw_down_minutes']:>4} min, counted {u['downtime_minutes']:>4} min{ex}, "
                         f"uptime ({u['eligible_minutes']} - {u['downtime_minutes']}) / {u['eligible_minutes']} = {u['uptime_pct']:.4f}%")
        if s["worst_component"]:
            lines.append(f"    worst covered component: {s['worst_component']} at {s['uptime_pct']:.4f}% -> tier {s['tier_pct']}%")
        lines.append(f"    fee {fmt_money(s['monthly_fee'], c.currency)} x {s['days']}/{mc['days_in_month']} = "
                     f"{fmt_money(s['prorated_fee'], c.currency)}; credit {s['tier_pct']}% = {fmt_money(s['credit'], c.currency)}")
    lines.append(f"  total credit {fmt_money(mc['credit'], c.currency)} (USD {mc['credit_usd_cents'] / 100:,.2f} at {mc['fx_to_usd']})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="slacredits")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("api")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8874)
    h = sub.add_parser("healthcheck")
    h.add_argument("--url", default="http://127.0.0.1:8874/healthz")
    m = sub.add_parser("month", help="explain one customer month")
    m.add_argument("customer")
    m.add_argument("month")
    cmp_ = sub.add_parser("compare", help="one month under different contract definitions")
    cmp_.add_argument("month")
    cmp_.add_argument("--component", default="api")
    sub.add_parser("totals", help="the year's credit totals")
    e = sub.add_parser("export", help="credit memo export for one month")
    e.add_argument("month")
    e.add_argument("--format", choices=["csv", "json"], default="csv")
    g = sub.add_parser("gate", help="fail if any credit moved without a contract or data change")
    g.add_argument("--golden", default=DEFAULT_GOLDEN)
    w = sub.add_parser("golden", help="write the golden credit file")
    w.add_argument("--out", default=DEFAULT_GOLDEN)
    s = sub.add_parser("summary", help="write a JSON summary of the year")
    s.add_argument("--out", default="docs/summary.json")
    args = ap.parse_args(argv)

    if args.cmd == "healthcheck":
        try:
            with urllib.request.urlopen(args.url, timeout=3) as r:
                return 0 if r.status == 200 else 1
        except OSError:
            return 1

    engine = _engine(args.seed)
    if args.cmd == "api":
        import uvicorn

        from slacredits.api import create_app

        uvicorn.run(create_app(engine, Store(args.db)), host=args.host, port=args.port, log_level="warning")
        return 0
    if args.cmd == "month":
        print(explain(engine, args.customer, args.month))
        return 0
    if args.cmd == "compare":
        print(f"{args.month} {args.component}: same probes, different contract definitions")
        for r in engine.compare(args.month, args.component):
            print(f"  {r['definition']:<64} notice {r['maintenance_notice_days']}d: down {r['downtime_minutes']:>4} min, "
                  f"excluded {r['excluded_minutes']:>3}, uptime {r['uptime_pct']:.4f}%")
        return 0
    if args.cmd == "gate":
        from slacredits.gate import check

        res = check(engine, json.loads(Path(args.golden).read_text()))
        print(json.dumps({k: v for k, v in res.items() if k != "explained"} | {"explained": len(res["explained"])}, indent=1))
        for v in res["violations"][:20]:
            print(f"VIOLATION {v['customer_id']}: {v['old_credit']} -> {v['new_credit']} {v['currency']}", file=sys.stderr)
        return 0 if res["ok"] else 1
    if args.cmd == "golden":
        from slacredits.gate import write_golden

        snap = write_golden(engine, args.out)
        print(f"wrote {args.out}: {len(snap['customers'])} customers, data {snap['data_fingerprint']}")
        return 0

    from slacredits import finance

    store = _store(args.db, engine)
    if args.cmd == "export":
        if args.format == "csv":
            sys.stdout.write(finance.memos_csv(engine, store, args.month))
        else:
            print(json.dumps(finance.memos_json(engine, store, args.month), indent=1))
        return 0
    if args.cmd in ("totals", "summary"):
        ri = finance.revenue_impact(engine, store)
        acc = finance.accrual(engine, store)
        if args.cmd == "summary":
            from slacredits.claims import list_claims
            from slacredits.status import summary as status_summary

            counts: dict[str, int] = {}
            for c in list_claims(store, limit=100000):
                counts[c["status"]] = counts.get(c["status"], 0) + 1
            out = {"as_of": engine.as_of.isoformat(), "seed": engine.ds.seed, "data_fingerprint": engine.ds.data_fingerprint(),
                   "months": MONTHS, "revenue_impact": ri, "accrual": {k: v for k, v in acc.items() if k != "rows"},
                   "claims_by_status": counts,
                   "disputes": {d: n for d, n in ((r["decision"] or "open", r["n"]) for r in store.q(
                       "SELECT decision, COUNT(*) n FROM disputes GROUP BY decision"))},
                   "status": {c: v for c, v in status_summary(engine.ds)["components"].items()}}
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(out, indent=1) + "\n")
            print(f"wrote {args.out}")
            return 0
        print(f"{'segment':<12} {'fees USD':>15} {'computed':>13} {'issued':>13} {'accrued':>11} {'forfeited':>12} {'issued %':>9}")
        for r in [*ri["segments"], {"segment": "total", **ri["total"]}]:
            print(f"{r['segment']:<12} {r['fee_usd_cents'] / 100:>15,.2f} {r['computed_usd_cents'] / 100:>13,.2f} "
                  f"{r['issued_usd_cents'] / 100:>13,.2f} {r['accrued_usd_cents'] / 100:>11,.2f} "
                  f"{r['forfeited_usd_cents'] / 100:>12,.2f} {r['issued_pct_of_fees']:>8.2f}%")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
