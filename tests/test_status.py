from slacredits import status


def test_day_status_thresholds():
    assert status.day_status(60, 0, 0) == "major_outage"
    assert status.day_status(10, 0, 0) == "partial_outage"
    assert status.day_status(1, 0, 0) == "degraded"
    assert status.day_status(0, 15, 0) == "degraded"
    assert status.day_status(0, 14, 30) == "maintenance"
    assert status.day_status(0, 3, 0) == "operational"


def test_history_covers_the_year(ds):
    h = status.history(ds, "api")
    assert len(h) == 365 and h[0]["date"] == "2025-11-01" and h[-1]["date"] == "2026-10-31"


def test_major_incident_day(ds):
    day = next(d for d in status.history(ds, "api") if d["date"] == "2026-03-17")
    assert day["status"] == "major_outage" and day["down_minutes"] >= 190
    assert any(i.id in day["incidents"] and i.severity == "critical" for i in ds.incidents)


def test_maintenance_days_are_marked_and_excluded(ds):
    h = {d["date"]: d for d in status.history(ds, "dashboard")}
    m = ds.maintenance[0]
    from slacredits.model import from_minute

    d = h[from_minute(m.start).date().isoformat()]
    assert d["maintenance_minutes"] >= m.end - m.start
    assert d["uptime_pct"] <= 100


def test_history_down_minutes_match_probes(ds):
    h = status.history(ds, "export")
    maint = {m for w in ds.maintenance if "export" in w.components for m in range(w.start, w.end)}
    from slacredits.model import month_bounds

    lo, _ = month_bounds("2025-11")
    _, hi = month_bounds("2026-10")
    expected = len([m for m in ds.probes.down_minutes("export", 2, lo, hi) if m not in maint])
    assert sum(d["down_minutes"] for d in h) == expected


def test_summary_hides_tenant_identity(ds):
    s = status.summary(ds)
    cc = [i for i in s["incidents"] if i["root_cause"] == "customer_caused"]
    assert cc and all("caused_by" not in i for i in cc)
    assert cc[0]["public_title"] == "Export delays for some customers"


def test_summary_uptime_reasonable(ds):
    s = status.summary(ds)
    for c, v in s["components"].items():
        assert 99.0 < v["uptime_pct_year"] < 100, c
        assert sum(v["days"].values()) == 365
