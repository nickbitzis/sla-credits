from fractions import Fraction

from conftest import version

from slacredits.credits import credit_amount, fmt_money, prorate, round_half_up, tier_pct, to_usd_cents


def up(pct: str) -> Fraction:
    return Fraction(pct) / 100


def test_exactly_at_target_earns_nothing():
    assert tier_pct(version(target=99.9), up("99.9")) == 0


def test_just_below_target_first_tier():
    assert tier_pct(version(target=99.9), up("99.8999")) == 10


def test_below_99_second_tier():
    assert tier_pct(version(target=99.9), up("98.99")) == 25


def test_below_95_third_tier():
    assert tier_pct(version(target=99.9), up("90")) == 50


def test_cap_limits_tier():
    assert tier_pct(version(target=99.9, cap_pct=30), up("90")) == 30


def test_9995_tiers():
    v = version(target=99.95)
    assert [tier_pct(v, up(x)) for x in ("99.95", "99.94", "99.0", "98.9")] == [0, 10, 10, 25]


def test_9999_tiers():
    v = version(target=99.99)
    assert [tier_pct(v, up(x)) for x in ("99.99", "99.98", "99.89", "98.5")] == [0, 10, 25, 50]


def test_exact_fraction_no_float_rounding():
    # 43 minutes down of 43,200 is 99.90046...%: above 99.9 so no credit, 44 minutes is below
    v = version(target=99.9)
    assert tier_pct(v, Fraction(43200 - 43, 43200)) == 0
    assert tier_pct(v, Fraction(43200 - 44, 43200)) == 10


def test_prorate_by_day():
    assert prorate(3_100_000, 10, 31) == 1_000_000
    assert prorate(1_000_000, 30, 30) == 1_000_000


def test_prorate_rounds_half_up_once():
    assert prorate(100, 1, 8) == 13  # 12.5 -> 13
    assert prorate(1_000_000, 11, 30) == 366_667


def test_credit_amount():
    assert credit_amount(366_667, 10) == 36_667
    assert credit_amount(0, 25) == 0


def test_round_half_up():
    assert round_half_up(Fraction(5, 2)) == 3 and round_half_up(Fraction(-5, 2)) == -3


def test_fx_conversion():
    assert to_usd_cents(10_000, "EUR", {"EUR": 1.0812}) == 10_812
    assert to_usd_cents(10_000, "USD", {"USD": 1.0}) == 10_000


def test_fmt_money():
    assert fmt_money(123456, "USD") == "$1,234.56"
    assert fmt_money(5, "GBP") == "GBP 0.05"


def test_mid_month_change_prorates_each_segment(engine, ds):
    c = next(c for c in ds.customers.values() if len(c.versions) == 2 and c.versions[1].effective.day > 1)
    m = c.versions[1].effective.strftime("%Y-%m")
    mc = engine.month(c.id, m)
    assert len(mc["segments"]) == 2
    assert sum(s["days"] for s in mc["segments"]) == mc["days_in_month"]
    for s in mc["segments"]:
        assert s["prorated_fee"] == prorate(s["monthly_fee"], s["days"], mc["days_in_month"])
        assert s["credit"] == credit_amount(s["prorated_fee"], s["tier_pct"])
    assert mc["credit"] == sum(s["credit"] for s in mc["segments"])


def test_major_incident_month_credits_most_customers(engine):
    rows = [r for r in engine.all_month("2026-03") if r["active"]]
    assert sum(1 for r in rows if r["credit"] > 0) / len(rows) > 0.9


def test_credit_never_exceeds_cap(engine, ds):
    for month in ("2026-01", "2026-03", "2026-05"):
        for r in engine.all_month(month):
            for s in r["segments"]:
                v = ds.customers[r["customer_id"]].versions[s["version"] - 1]
                assert s["tier_pct"] <= v.cap_pct
