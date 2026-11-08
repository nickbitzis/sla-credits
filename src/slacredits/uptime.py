"""Contract-accurate uptime: down minutes, exclusions with evidence, and segments.

Uptime for one component over [start, end):
    eligible = minutes in range - excluded minutes
    uptime   = (eligible - downtime) / eligible
Excluded minutes are neither up nor down. Comparisons against targets use
exact fractions, so 99.95% never rounds into compliance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

from slacredits.model import ContractVersion, Customer, Incident, Maintenance, day_minute, iso, month_bounds
from slacredits.probes import ProbeData, window_down


@dataclass(frozen=True)
class Exclusion:
    kind: str  # maintenance, force_majeure, customer_caused
    ref: str
    start: int
    end: int
    applied: bool
    reason: str
    evidence: dict = field(default_factory=dict, hash=False, compare=False)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "start": iso(self.start), "end": iso(self.end),
                "minutes": self.end - self.start, "applied": self.applied, "reason": self.reason, "evidence": self.evidence}


@dataclass(frozen=True)
class ComponentUptime:
    component: str
    start: int
    end: int
    total: int
    excluded: int
    eligible: int
    raw_down: int  # down minutes by the quorum rule before exclusions
    downtime: int  # minutes counted against the SLA
    down_windows: int
    exclusions: tuple[str, ...]

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.eligible - self.downtime, self.eligible) if self.eligible else Fraction(1)

    @property
    def pct(self) -> float:
        return round(float(self.fraction * 100), 4)

    def to_dict(self) -> dict:
        return {"component": self.component, "start": iso(self.start), "end": iso(self.end), "total_minutes": self.total,
                "excluded_minutes": self.excluded, "eligible_minutes": self.eligible, "raw_down_minutes": self.raw_down,
                "downtime_minutes": self.downtime, "down_windows": self.down_windows, "uptime_pct": self.pct,
                "exclusions_applied": list(self.exclusions)}


def exclusions_for(v: ContractVersion, customer_id: str, component: str, start: int, end: int,
                   maintenance: list[Maintenance], incidents: list[Incident]) -> list[Exclusion]:
    """Every exclusion candidate that touches the range, applied or not, with the reason."""
    out: list[Exclusion] = []
    for m in maintenance:
        if component not in m.components or m.end <= start or m.start >= end:
            continue
        notice = m.notice_days()
        ok = notice >= v.maintenance_notice_days
        reason = (f"announced {notice:g} days ahead, contract requires {v.maintenance_notice_days}" if ok else
                  f"announced only {notice:g} days ahead, contract requires {v.maintenance_notice_days}: counts as downtime")
        out.append(Exclusion("maintenance", m.id, max(m.start, start), min(m.end, end), ok, reason,
                             {"title": m.title, "announced": iso(m.announced), "scheduled_start": iso(m.start),
                              "scheduled_end": iso(m.end), "notice_days": notice, "required_days": v.maintenance_notice_days}))
    for inc in incidents:
        if component not in inc.components or inc.end <= start or inc.start >= end:
            continue
        if inc.root_cause == "force_majeure":
            out.append(Exclusion("force_majeure", inc.id, max(inc.start, start), min(inc.end, end), True,
                                 "force majeure clause: event outside the vendor's reasonable control", {"title": inc.title}))
        elif inc.root_cause == "customer_caused":
            mine = inc.caused_by == customer_id
            out.append(Exclusion("customer_caused", inc.id, max(inc.start, start), min(inc.end, end), mine,
                                 "caused by this customer's own usage" if mine else "caused by another tenant: counts as downtime",
                                 {"title": inc.title}))
    return out


def component_uptime(probes: ProbeData, v: ContractVersion, component: str, start: int, end: int,
                     exclusions: list[Exclusion]) -> ComponentUptime:
    applied = [e for e in exclusions if e.applied]
    excluded: set[int] = set()
    for e in applied:
        excluded.update(range(max(e.start, start), min(e.end, end)))
    down = set(probes.down_minutes(component, v.quorum, start, end))
    total = end - start
    eligible = total - len(excluded)
    if v.measurement == "minute":
        downtime, windows = len(down - excluded), 0
    else:
        downtime, windows = window_down(down, excluded, start, end, v.window_down_minutes)
    return ComponentUptime(component, start, end, total, len(excluded), eligible, len(down), downtime, windows,
                           tuple(e.ref for e in applied))


@dataclass(frozen=True)
class Segment:
    version: ContractVersion
    start: int
    end: int
    days: int


def segments(c: Customer, month: str) -> list[Segment]:
    """Split a month at contract changes (and at the service start). Boundaries fall on day starts."""
    lo, hi = month_bounds(month)
    out: list[Segment] = []
    vs = c.versions
    for i, v in enumerate(vs):
        s = max(lo, day_minute(max(v.effective, c.start)))
        e = min(hi, day_minute(vs[i + 1].effective)) if i + 1 < len(vs) else hi
        if e > s:
            out.append(Segment(v, s, e, (e - s) // 1440))
    return out


def active_on(c: Customer, d: date) -> ContractVersion | None:
    cur = None
    for v in c.versions:
        if v.effective <= d:
            cur = v
    return cur if d >= c.start else None
