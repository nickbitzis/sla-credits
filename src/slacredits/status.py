"""Public status page history, derived from the same probes as the contracts.

The public definition: a minute is down when 2 of 3 regions fail, and a minute
is degraded when exactly 1 region fails. Announced maintenance minutes are
shown as maintenance and left out of the public uptime figure.

Daily status: major_outage (60+ down minutes), partial_outage (10+),
degraded (any down minute, or 15+ degraded minutes), maintenance (only
maintenance), operational.
"""

from __future__ import annotations

from datetime import timedelta

from slacredits.model import COMPONENTS, EPOCH, MONTHS, iso, month_bounds
from slacredits.probes import popcount
from slacredits.synth import Dataset

PUBLIC_QUORUM = 2


def day_status(down: int, degraded: int, maint: int) -> str:
    if down >= 60:
        return "major_outage"
    if down >= 10:
        return "partial_outage"
    if down > 0 or degraded >= 15:
        return "degraded"
    if maint > 0:
        return "maintenance"
    return "operational"


def history(ds: Dataset, component: str) -> list[dict]:
    if component not in COMPONENTS:
        raise KeyError(component)
    lo, _ = month_bounds(MONTHS[0])
    _, hi = month_bounds(MONTHS[-1])
    n_days = (hi - lo) // 1440
    maint = set()
    for m in ds.maintenance:
        if component in m.components:
            maint.update(range(m.start, m.end))
    days = [{"down": 0, "degraded": 0, "maint": 0} for _ in range(n_days)]
    f = ds.probes.failures[component]
    for minute in ds.probes.failing_minutes(component):
        d = days[(minute - lo) // 1440]
        if minute in maint:
            d["maint"] += 1
        elif popcount(f[minute]) >= PUBLIC_QUORUM:
            d["down"] += 1
        else:
            d["degraded"] += 1
    by_day: dict[int, list[str]] = {}
    for inc in ds.incidents:
        if component in inc.components and inc.root_cause != "customer_caused":
            for day in range((inc.start - lo) // 1440, (inc.end - 1 - lo) // 1440 + 1):
                by_day.setdefault(day, []).append(inc.id)
    for inc in ds.incidents:  # tenant-specific causes are shown without naming the tenant
        if component in inc.components and inc.root_cause == "customer_caused":
            by_day.setdefault((inc.start - lo) // 1440, []).append(inc.id)
    out = []
    for i, d in enumerate(days):
        eligible = 1440 - d["maint"]
        out.append({"date": (EPOCH + timedelta(days=i)).date().isoformat(), "status": day_status(d["down"], d["degraded"], d["maint"]),
                    "down_minutes": d["down"], "degraded_minutes": d["degraded"], "maintenance_minutes": d["maint"],
                    "uptime_pct": round(100 * (eligible - d["down"]) / eligible, 3), "incidents": by_day.get(i, [])})
    return out


def summary(ds: Dataset) -> dict:
    comps = {}
    for c in COMPONENTS:
        h = history(ds, c)
        down = sum(d["down_minutes"] for d in h)
        elig = sum(1440 - d["maintenance_minutes"] for d in h)
        comps[c] = {"uptime_pct_year": round(100 * (elig - down) / elig, 4), "down_minutes": down,
                    "days": {s: sum(1 for d in h if d["status"] == s) for s in
                             ("operational", "maintenance", "degraded", "partial_outage", "major_outage")}}
    incidents = [{**i.public(), "public_title": i.title if i.root_cause != "customer_caused" else "Export delays for some customers"}
                 for i in ds.incidents]
    maint = [{"id": m.id, "components": list(m.components), "start": iso(m.start), "end": iso(m.end), "announced": iso(m.announced),
              "notice_days": m.notice_days(), "title": m.title} for m in ds.maintenance]
    return {"definition": "2 of 3 regions failing is down, 1 of 3 is degraded; announced maintenance excluded",
            "components": comps, "incidents": incidents, "maintenance": maint}
