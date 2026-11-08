import csv
import io
from datetime import date

from helpers import MONTH, claim_customer, in_window

from slacredits import claims, finance
from slacredits.model import MONTHS


def test_csv_and_json_totals_agree(engine, seeded):
    js = finance.memos_json(engine, seeded, MONTH)
    rows = list(csv.DictReader(io.StringIO(finance.memos_csv(engine, seeded, MONTH))))
    assert len(rows) == js["totals"]["count"] > 0
    usd = round(sum(float(r["amount_usd"]) for r in rows) * 100)
    assert usd == js["totals"]["usd_cents"] == sum(m["amount_usd"] for m in js["memos"])
    for cur, total in js["totals"]["by_currency"].items():
        assert round(sum(float(r["amount"]) for r in rows if r["currency"] == cur) * 100) == total


def test_proactive_memos_equal_computed_credits(engine, seeded):
    memos = [m for m in finance.memos(engine, seeded, MONTH) if m["basis"] == "proactive"]
    expected = [r for r in engine.all_month(MONTH) if r["basis"] == "proactive" and r["credit"] > 0]
    assert len(memos) == len(expected)
    assert sum(m["amount_usd"] for m in memos) == sum(r["credit_usd_cents"] for r in expected)


def test_proactive_issued_three_days_after_month_end(engine, seeded):
    assert all(m["issued_on"] == "2026-04-03" for m in finance.memos(engine, seeded, MONTH) if m["basis"] == "proactive")
    assert not [m for m in finance.memos(engine, seeded, MONTH, as_of=date(2026, 4, 2)) if m["basis"] == "proactive"]


def test_claim_memos_only_for_approved(engine, seeded):
    ids = {m["claim_id"] for m in finance.memos(engine, seeded, MONTH) if m["basis"] == "claim"}
    approved = {c["id"] for c in claims.list_claims(seeded, "approved", limit=10000) if c["month"] == MONTH and c["credit"] > 0}
    assert ids == approved


def test_memo_csv_header(engine, seeded):
    assert finance.memos_csv(engine, seeded, MONTH).splitlines()[0].split(",") == finance.MEMO_FIELDS


def test_accrual_unclaimed_uses_claim_rate(engine, store):
    acc = finance.accrual(engine, store, as_of=date(2026, 4, 10))
    rows = [r for r in acc["rows"] if r["month"] == MONTH and r["kind"] == "unclaimed_window_open"]
    assert rows
    for r in rows:
        assert r["probability"] == engine.ds.claim_rate[r["segment"]]
        assert r["expected_usd_cents"] == round(r["credit_usd_cents"] * r["probability"])


def test_accrual_proactive_pending_before_issue(engine, store):
    acc = finance.accrual(engine, store, as_of=date(2026, 4, 2))
    pend = [r for r in acc["rows"] if r["kind"] == "proactive_pending"]
    assert pend and all(r["month"] == MONTH for r in pend)


def test_accrual_drops_after_window_closes(engine, store):
    acc = finance.accrual(engine, store, as_of=date(2026, 7, 1))
    assert not [r for r in acc["rows"] if r["month"] == MONTH]


def test_accrual_counts_open_claim_at_full_value(engine, store):
    cid, mc = claim_customer(engine)
    claims.submit(engine, store, cid, MONTH, [mc["segments"][0]["worst_component"]], mc["downtime_minutes"], in_window())
    acc = finance.accrual(engine, store, as_of=in_window(days=6))
    row = next(r for r in acc["rows"] if r["customer_id"] == cid and r["month"] == MONTH)
    assert row["kind"] == "claim_open" and row["expected_usd_cents"] == mc["credit_usd_cents"]


def test_accrual_total_is_sum(engine, seeded):
    acc = finance.accrual(engine, seeded)
    assert acc["total_usd_cents"] == sum(r["expected_usd_cents"] for r in acc["rows"]) == sum(acc["by_kind"].values())


def test_revenue_impact_adds_up(engine, seeded):
    ri = finance.revenue_impact(engine, seeded)
    assert ri["total"]["issued_usd_cents"] == sum(m["issued_usd_cents"] for m in ri["months"])
    assert ri["total"]["issued_usd_cents"] == sum(finance.memos_json(engine, seeded, m)["totals"]["usd_cents"] for m in MONTHS)
    assert ri["total"]["fee_usd_cents"] == sum(r["fee_usd_cents"] for r in ri["segments"])
    ent = next(r for r in ri["segments"] if r["segment"] == "enterprise")
    assert ent["forfeited_usd_cents"] == 0  # proactive contracts never forfeit
    for r in ri["segments"]:
        assert r["issued_usd_cents"] + r["forfeited_usd_cents"] <= r["computed_usd_cents"] + r["fee_usd_cents"] // 10
