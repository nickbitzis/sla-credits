"""Seeded synthetic data for Corvane, a made-up B2B analytics SaaS vendor.

300 customers on three contract families, 12 months of per-minute probes from
3 regions for 3 components, planned maintenance with varying notice, and
incident records with root cause tags. Same seed, same bytes.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from slacredits.model import (
    COMPONENTS,
    MONTHS,
    REGIONS,
    ContractVersion,
    CreditTier,
    Customer,
    Incident,
    Maintenance,
    day_minute,
    month_bounds,
    month_start,
    to_minute,
)
from slacredits.probes import ProbeData

VENDOR = "Corvane"
DEFAULT_SEED = 20261

TIERS = {
    99.9: (CreditTier(99.9, 10), CreditTier(99.0, 25), CreditTier(95.0, 50)),
    99.95: (CreditTier(99.95, 10), CreditTier(99.0, 25), CreditTier(95.0, 50)),
    99.99: (CreditTier(99.99, 10), CreditTier(99.9, 25), CreditTier(99.0, 50)),
}

_FIRST = ["Alder", "Brightwater", "Cobalt", "Dunmore", "Elmstead", "Fernhill", "Garnet", "Harrow", "Ironleaf", "Juniper",
          "Kestrel", "Larkspur", "Marlow", "Northgate", "Oakfield", "Pinecrest", "Quarry", "Redfern", "Saltmarsh", "Thistle",
          "Umber", "Vantor", "Willowby", "Yarrow", "Zephyr"]
_SECOND = ["Labs", "Logistics", "Health", "Foods", "Freight", "Analytics", "Retail", "Energy", "Capital", "Studios",
           "Robotics", "Clinics", "Outdoors"]

ROOT_TITLES = {
    "deploy": ("Elevated errors after a deploy", "A release raised error rates. It was rolled back."),
    "config": ("Configuration change caused failures", "A bad load balancer configuration was pushed and reverted."),
    "dependency": ("Upstream dependency degraded", "A managed queue in the data path slowed down and timed out requests."),
    "capacity": ("Capacity exhausted under load", "Traffic exceeded autoscaling limits until new nodes joined."),
    "network": ("Regional network loss", "Packet loss between the edge and the application tier."),
}


@dataclass
class Dataset:
    seed: int
    customers: dict[str, Customer]
    probes: ProbeData
    maintenance: list[Maintenance]
    incidents: list[Incident]
    fx_to_usd: dict[str, dict[str, float]]  # month -> currency -> rate
    claim_rate: dict[str, float] = field(default_factory=dict)

    def data_fingerprint(self) -> str:
        h = hashlib.sha256(self.probes.fingerprint().encode())
        h.update(json.dumps([vars(m) for m in self.maintenance], sort_keys=True, default=str).encode())
        h.update(json.dumps([vars(i) for i in self.incidents], sort_keys=True, default=str).encode())
        h.update(json.dumps(self.fx_to_usd, sort_keys=True).encode())
        return h.hexdigest()[:16]


def contract_fingerprint(c: Customer) -> str:
    return hashlib.sha256(json.dumps([v.terms() for v in c.versions], sort_keys=True).encode()).hexdigest()[:16]


def _fee(rng: random.Random, lo: int, hi: int, currency: str) -> int:
    rate = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79}[currency]
    return int(round(rng.uniform(lo, hi) * rate / 50) * 50) * 100


def _terms(rng: random.Random, cid: str, segment: str, currency: str, version: int, effective: date,
           prev: ContractVersion | None = None) -> ContractVersion:
    if segment == "smb":
        target = 99.9
        measurement = rng.choice(["minute", "window5"])
        quorum = 3 if rng.random() < 0.2 else 2
        components = ("api",) if rng.random() < 0.5 else ("api", "dashboard")
        fee, cap, window, notice, proactive = _fee(rng, 400, 3000, currency), 50, 30, 5, False
    elif segment == "mid_market":
        target = rng.choice([99.9, 99.95])
        measurement = rng.choice(["minute", "window5"])
        quorum = 2
        components = ("api", "dashboard", "export") if rng.random() < 0.4 else ("api", "dashboard")
        fee, cap, window, notice, proactive = _fee(rng, 3000, 20000, currency), 50, rng.choice([30, 45]), rng.choice([5, 7]), False
    else:
        target = rng.choice([99.95, 99.95, 99.99])
        measurement = rng.choice(["minute", "minute", "window5"])
        quorum = 1 if target == 99.95 and rng.random() < 0.25 else 2
        components = ("api", "dashboard", "export") if rng.random() < 0.7 else ("api", "export")
        fee, cap, window, notice, proactive = _fee(rng, 25000, 150000, currency), rng.choice([30, 50]), 60, 7, True
    if prev is not None:  # amendment: higher fee, sometimes a stricter target or more components
        fee = int(round(prev.monthly_fee * rng.uniform(1.1, 1.6) / 5000) * 5000) or prev.monthly_fee
        if prev.target == 99.9 and rng.random() < 0.5:
            target = 99.95
        else:
            target = prev.target
        components = tuple(c for c in COMPONENTS if c in set(prev.components) | ({"dashboard"} if rng.random() < 0.5 else set()))
        measurement, quorum, cap, window, notice, proactive = (
            prev.measurement, prev.quorum, prev.cap_pct, prev.claim_window_days, prev.maintenance_notice_days, prev.proactive)
    return ContractVersion(
        contract_id=f"K-{cid}", version=version, effective=effective, monthly_fee=fee, currency=currency,
        target=target, measurement=measurement, window_down_minutes=rng.choice([1, 3]) if measurement == "window5" else 0,
        quorum=quorum, components=components, tiers=TIERS[target], cap_pct=cap, claim_window_days=window,
        maintenance_notice_days=notice, proactive=proactive,
    )


def make_customers(rng: random.Random, n: int = 300) -> dict[str, Customer]:
    ids = [f"c{i:03d}" for i in range(1, n + 1)]
    segs = ["enterprise"] * (n * 15 // 100) + ["mid_market"] * (n * 35 // 100)
    segs += ["smb"] * (n - len(segs))
    rng.shuffle(segs)
    names = [f"{a} {b}" for a in _FIRST for b in _SECOND]
    rng.shuffle(names)
    out: dict[str, Customer] = {}
    year_start = date(2025, 11, 1)
    for cid, seg, name in zip(ids, segs, names, strict=False):
        currency = rng.choices(["USD", "EUR", "GBP"], weights=[60, 25, 15])[0]
        if rng.random() < 0.12:
            start = year_start + timedelta(days=rng.randint(20, 300))
        else:
            start = date(2024, rng.randint(1, 12), rng.randint(1, 28))
        cust = Customer(cid, name, seg, currency, start)
        v1 = _terms(rng, cid, seg, currency, 1, start)
        cust.versions.append(v1)
        if rng.random() < 0.12:
            first = max(start + timedelta(days=30), year_start + timedelta(days=15))
            eff = first + timedelta(days=rng.randint(0, 280))
            if eff.day == 1:
                eff += timedelta(days=rng.randint(3, 20))
            if eff < date(2026, 10, 25):
                cust.versions.append(_terms(rng, cid, seg, currency, 2, eff, prev=v1))
        out[cid] = cust
    return out


def _sunday_2am(month: str, rng: random.Random) -> int:
    d = month_start(month).date()
    sundays = [d + timedelta(days=i) for i in range(28) if (d + timedelta(days=i)).weekday() == 6]
    return day_minute(rng.choice(sundays)) + 120


def generate(seed: int = DEFAULT_SEED, n_customers: int = 300) -> Dataset:
    rng = random.Random(seed)
    customers = make_customers(rng, n_customers)
    probes = ProbeData()
    maint: list[Maintenance] = []
    incidents: list[Incident] = []
    inc_n = 0

    def add_incident(components, regions, start, end, severity, cause, title=None, summary=None, caused_by=None):
        nonlocal inc_n
        inc_n += 1
        t, s = ROOT_TITLES.get(cause, (title, summary))
        incidents.append(Incident(f"INC-{inc_n:04d}", tuple(components), tuple(regions), start, end, severity, cause,
                                  title or t, summary or s, caused_by))

    for mi, month in enumerate(MONTHS):
        lo, hi = month_bounds(month)
        for comp in COMPONENTS:
            # regional blips: one region, 1 to 6 minutes, no incident record
            for _ in range(rng.randint(6, 14)):
                s = rng.randrange(lo, hi - 10)
                probes.fail(comp, rng.choice(REGIONS), s, s + rng.randint(1, 4))
            # partial outages: two regions, solid or flapping
            for _ in range(rng.choices([0, 1, 2, 3], weights=[3, 4, 2, 1] if comp == "api" else [5, 4, 1, 0])[0]):
                s = rng.randrange(lo, hi - 120)
                dur = rng.choice([3, 4, 5, 6, 8, 10, 12, 15, 18, 25])
                regs = rng.sample(REGIONS, 2)
                flap = rng.random() < 0.4
                m = s
                while m < s + dur:
                    chunk = rng.randint(1, 3)
                    if not flap or rng.random() < 0.6:
                        for r in regs:
                            probes.fail(comp, r, m, min(m + chunk, s + dur))
                    m += chunk
                cause = rng.choice(["deploy", "config", "dependency", "capacity"])
                add_incident([comp], regs, s, s + dur, "minor" if dur < 20 else "major", cause)
            # occasional long single-region outage
            if rng.random() < 0.25:
                s = rng.randrange(lo, hi - 120)
                dur = rng.randint(20, 90)
                reg = rng.choice(REGIONS)
                probes.fail(comp, reg, s, s + dur)
                add_incident([comp], [reg], s, s + dur, "minor", "network")
        # planned maintenance: dashboard and export monthly, api every third month
        comps = ("dashboard", "export") + (("api",) if mi % 3 == 1 else ())
        s = _sunday_2am(month, rng)
        dur = rng.choice([20, 30, 30, 45])
        notice = rng.choice([14, 10, 10, 8, 7, 7, 7, 6])
        mid = f"MW-{month}"
        maint.append(Maintenance(mid, comps, s, s + dur, s - notice * 1440 - rng.randint(0, 600),
                                 f"Database upgrade window ({', '.join(comps)})"))
        overrun = rng.choice([0, 0, 0, 0, 8, 22])
        for comp in comps:
            for r in REGIONS:
                probes.fail(comp, r, s, s + dur + overrun)
        if overrun:
            add_incident(comps, REGIONS, s + dur, s + dur + overrun, "minor", "maintenance_overrun",
                         "Maintenance ran past its window", f"{mid} finished {overrun} minutes after the announced end.")

    # emergency maintenance with one day of notice
    s = to_minute(month_start("2026-01")) + 9 * 1440 + 4 * 60
    maint.append(Maintenance("MW-2026-01-E", ("api",), s, s + 40, s - 1440, "Emergency certificate rotation (api)"))
    for r in REGIONS:
        probes.fail("api", r, s, s + 40)

    # the major incident of the year
    s = to_minute(month_start("2026-03")) + 16 * 1440 + 14 * 60 + 5
    for r in REGIONS:
        probes.fail("api", r, s, s + 190)
        probes.fail("export", r, s, s + 190)
    for r in REGIONS[:2]:
        probes.fail("dashboard", r, s + 20, s + 140)
    add_incident(COMPONENTS, REGIONS, s, s + 190, "critical", "dependency", "Storage cluster failover stalled",
                 "The primary storage cluster lost quorum and the automatic failover stalled. API and export were down in "
                 "all regions for 3 hours 10 minutes; the dashboard served cached pages in ap-south.")

    # force majeure: subsea cable cut, two regions
    s = to_minute(month_start("2026-07")) + 8 * 1440 + 6 * 60 + 20
    for comp in ("api", "dashboard"):
        for r in ("eu-west", "ap-south"):
            probes.fail(comp, r, s, s + 55)
    add_incident(("api", "dashboard"), ("eu-west", "ap-south"), s, s + 55, "major", "force_majeure",
                 "Subsea cable cut isolated two regions", "Two carrier cables were cut by a ship anchor. Traffic rerouted after 55 minutes.")

    # customer caused: one enterprise customer's runaway export job saturated the export workers
    culprit = next(c.id for c in customers.values() if c.segment == "enterprise" and "export" in c.versions[0].components
                   and c.start < date(2026, 5, 1))
    s = to_minute(month_start("2026-05")) + 20 * 1440 + 15 * 60
    for r in ("us-east", "eu-west"):
        probes.fail("export", r, s, s + 35)
    add_incident(("export",), ("us-east", "eu-west"), s, s + 35, "major", "customer_caused",
                 "Export workers saturated by one tenant", "A single tenant's misconfigured job submitted 40,000 exports in 10 minutes.",
                 caused_by=culprit)

    incidents.sort(key=lambda i: i.start)
    incidents = [replace(inc, id=f"INC-{n:04d}") for n, inc in enumerate(incidents, 1)]
    fx: dict[str, dict[str, float]] = {}
    eur, gbp = 1.08, 1.27
    for month in MONTHS:
        eur = round(eur * rng.uniform(0.985, 1.015), 4)
        gbp = round(gbp * rng.uniform(0.985, 1.015), 4)
        fx[month] = {"USD": 1.0, "EUR": eur, "GBP": gbp}
    return Dataset(seed, customers, probes, maint, incidents, fx,
                   claim_rate={"smb": 0.30, "mid_market": 0.50, "enterprise": 1.0})
