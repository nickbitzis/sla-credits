from datetime import date
from fractions import Fraction

from conftest import version

from slacredits.model import Customer, Incident, Maintenance, day_minute, month_bounds
from slacredits.probes import ProbeData
from slacredits.uptime import component_uptime, exclusions_for, segments

LO, HI = month_bounds("2026-04")  # 30 days, 43,200 minutes


def outage(start, end, regions=("us-east", "eu-west", "ap-south")):
    p = ProbeData()
    for r in regions:
        p.fail("api", r, start, end)
    return p


def maint(notice_days, start=LO + 1000, minutes=60):
    return Maintenance("MW-x", ("api",), start, start + minutes, start - int(notice_days * 1440), "test window")


def test_uptime_math_exact():
    u = component_uptime(outage(LO + 10, LO + 53), version(), "api", LO, HI, [])
    assert (u.total, u.eligible, u.downtime) == (43200, 43200, 43)
    assert u.fraction == Fraction(43200 - 43, 43200)
    assert u.pct == 99.9005


def test_maintenance_with_enough_notice_is_excluded():
    v = version(maintenance_notice_days=5)
    ex = exclusions_for(v, "c1", "api", LO, HI, [maint(5)], [])
    assert ex[0].applied
    u = component_uptime(outage(LO + 1000, LO + 1060), v, "api", LO, HI, ex)
    assert (u.excluded, u.downtime, u.eligible) == (60, 0, 43140)


def test_maintenance_with_short_notice_counts_as_downtime():
    v = version(maintenance_notice_days=7)
    ex = exclusions_for(v, "c1", "api", LO, HI, [maint(6.9)], [])
    assert not ex[0].applied and "counts as downtime" in ex[0].reason
    u = component_uptime(outage(LO + 1000, LO + 1060), v, "api", LO, HI, ex)
    assert u.downtime == 60 and u.excluded == 0


def test_notice_exactly_at_requirement_is_enough():
    ex = exclusions_for(version(maintenance_notice_days=7), "c1", "api", LO, HI, [maint(7)], [])
    assert ex[0].applied and ex[0].evidence["notice_days"] == 7


def test_maintenance_overrun_counts():
    v = version()
    ex = exclusions_for(v, "c1", "api", LO, HI, [maint(10)], [])
    u = component_uptime(outage(LO + 1000, LO + 1075), v, "api", LO, HI, ex)  # 15 minutes past the window
    assert u.downtime == 15 and u.excluded == 60


def test_maintenance_on_other_component_ignored():
    m = Maintenance("MW-d", ("dashboard",), LO + 10, LO + 40, LO - 20000, "dash")
    assert exclusions_for(version(), "c1", "api", LO, HI, [m], []) == []


def test_force_majeure_excluded_for_everyone():
    inc = Incident("INC-1", ("api",), ("eu-west", "ap-south"), LO + 500, LO + 555, "major", "force_majeure", "cable", "cut")
    ex = exclusions_for(version(), "anyone", "api", LO, HI, [], [inc])
    assert ex[0].applied and ex[0].kind == "force_majeure"


def test_customer_caused_excluded_only_for_that_customer():
    inc = Incident("INC-2", ("api",), ("us-east", "eu-west"), LO + 500, LO + 535, "major", "customer_caused", "t", "s", caused_by="c7")
    mine = exclusions_for(version(), "c7", "api", LO, HI, [], [inc])
    other = exclusions_for(version(), "c8", "api", LO, HI, [], [inc])
    assert mine[0].applied and not other[0].applied
    p = outage(LO + 500, LO + 535, ("us-east", "eu-west"))
    assert component_uptime(p, version(), "api", LO, HI, mine).downtime == 0
    assert component_uptime(p, version(), "api", LO, HI, other).downtime == 35


def test_minute_vs_window_on_flapping_outage():
    p = ProbeData()
    for m in range(LO + 100, LO + 140, 4):  # 1 down minute every 4 minutes, 2 regions
        p.fail("api", "us-east", m, m + 1)
        p.fail("api", "eu-west", m, m + 1)
    minute = component_uptime(p, version(measurement="minute"), "api", LO, HI, [])
    window = component_uptime(p, version(measurement="window5", window_down_minutes=1), "api", LO, HI, [])
    strict3 = component_uptime(p, version(measurement="window5", window_down_minutes=3), "api", LO, HI, [])
    assert minute.downtime == 10
    assert window.downtime == 40 and window.down_windows == 8
    assert strict3.downtime == 0


def test_quorum_changes_uptime_for_same_probes():
    p = outage(LO, LO + 30, ("us-east",))
    assert component_uptime(p, version(quorum=1), "api", LO, HI, []).downtime == 30
    assert component_uptime(p, version(quorum=2), "api", LO, HI, []).downtime == 0


def _cust(start=date(2024, 1, 1), change=None):
    c = Customer("c1", "Test Co", "smb", "USD", start, [version(effective=start)])
    if change:
        c.versions.append(version(version=2, effective=change, monthly_fee=2_000_000))
    return c


def test_segments_whole_month():
    s = segments(_cust(), "2026-04")
    assert len(s) == 1 and s[0].days == 30


def test_segments_split_on_mid_month_change():
    s = segments(_cust(change=date(2026, 4, 11)), "2026-04")
    assert [(x.version.version, x.days) for x in s] == [(1, 10), (2, 20)]
    assert s[1].start == day_minute(date(2026, 4, 11))


def test_segments_start_mid_month():
    s = segments(_cust(start=date(2026, 4, 21)), "2026-04")
    assert len(s) == 1 and s[0].days == 10


def test_segments_before_start_empty():
    assert segments(_cust(start=date(2026, 6, 1)), "2026-04") == []


def test_engine_month_uptime_is_worst_component(engine, ds):
    c = next(c for c in ds.customers.values() if len(c.versions[0].components) == 3 and len(c.versions) == 1 and c.start < date(2025, 11, 1))
    mc = engine.month(c.id, "2026-03")
    seg = mc["segments"][0]
    assert seg["uptime_pct"] == min(u["uptime_pct"] for u in seg["components"])
