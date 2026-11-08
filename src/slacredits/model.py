"""Domain model and the minute clock.

Time is an integer minute index from EPOCH (2025-11-01 00:00 UTC). The data
year runs 12 calendar months, Nov 2025 to Oct 2026. Money is integer minor
units (cents, euro cents, pence) in the contract's currency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

EPOCH = datetime(2025, 11, 1, tzinfo=UTC)
MONTHS = [
    "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04",
    "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10",
]
COMPONENTS = ("api", "dashboard", "export")
REGIONS = ("us-east", "eu-west", "ap-south")
SEGMENTS = ("smb", "mid_market", "enterprise")
CURRENCIES = ("USD", "EUR", "GBP")
DEMO_AS_OF = date(2026, 11, 8)


def to_minute(dt: datetime) -> int:
    return int((dt - EPOCH).total_seconds() // 60)


def from_minute(m: int) -> datetime:
    return EPOCH + timedelta(minutes=m)


def iso(m: int) -> str:
    return from_minute(m).strftime("%Y-%m-%dT%H:%MZ")


def month_start(month: str) -> datetime:
    y, mo = (int(x) for x in month.split("-"))
    return datetime(y, mo, 1, tzinfo=UTC)


def next_month(month: str) -> str:
    y, mo = (int(x) for x in month.split("-"))
    return f"{y + 1}-01" if mo == 12 else f"{y}-{mo + 1:02d}"


def month_bounds(month: str) -> tuple[int, int]:
    """[start, end) minute indexes of a calendar month."""
    return to_minute(month_start(month)), to_minute(month_start(next_month(month)))


def days_in_month(month: str) -> int:
    a, b = month_bounds(month)
    return (b - a) // 1440


def month_end_date(month: str) -> date:
    return (month_start(next_month(month)) - timedelta(days=1)).date()


def month_of_minute(m: int) -> str:
    return from_minute(m).strftime("%Y-%m")


def day_minute(d: date) -> int:
    return to_minute(datetime(d.year, d.month, d.day, tzinfo=UTC))


@dataclass(frozen=True)
class CreditTier:
    below: float  # uptime percent strictly below this earns `pct`
    pct: int  # percent of the (prorated) monthly fee


@dataclass(frozen=True)
class ContractVersion:
    """One version of a customer's SLA terms. A new version starts on `effective`."""

    contract_id: str
    version: int
    effective: date
    monthly_fee: int  # minor units
    currency: str
    target: float  # e.g. 99.95
    measurement: str  # "minute" or "window5"
    window_down_minutes: int  # window5 only: down minutes in a 5 minute window that make it down
    quorum: int  # regions that must fail at the same minute for the minute to count as down
    components: tuple[str, ...]
    tiers: tuple[CreditTier, ...]  # ordered by `below`, highest first
    cap_pct: int
    claim_window_days: int
    maintenance_notice_days: int
    proactive: bool  # credits issued without a claim

    def definition(self) -> str:
        rule = "per minute" if self.measurement == "minute" else f"5 minute windows (down if {self.window_down_minutes}+ down minutes)"
        return f"{self.quorum} of 3 regions failing, {rule}"

    def terms(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "effective": self.effective.isoformat(),
            "monthly_fee": self.monthly_fee,
            "currency": self.currency,
            "target": self.target,
            "measurement": self.measurement,
            "window_down_minutes": self.window_down_minutes,
            "quorum": self.quorum,
            "definition": self.definition(),
            "components": list(self.components),
            "tiers": [{"below": t.below, "pct": t.pct} for t in self.tiers],
            "cap_pct": self.cap_pct,
            "claim_window_days": self.claim_window_days,
            "maintenance_notice_days": self.maintenance_notice_days,
            "proactive": self.proactive,
            "credit_basis": "proactive" if self.proactive else "claim_required",
        }


@dataclass
class Customer:
    id: str
    name: str
    segment: str
    currency: str
    start: date  # first day of service
    versions: list[ContractVersion] = field(default_factory=list)

    def summary(self) -> dict:
        cur = self.versions[-1]
        return {
            "id": self.id, "name": self.name, "segment": self.segment, "currency": self.currency,
            "start": self.start.isoformat(), "target": cur.target, "monthly_fee": cur.monthly_fee,
            "components": list(cur.components), "proactive": cur.proactive, "versions": len(self.versions),
        }


@dataclass(frozen=True)
class Maintenance:
    id: str
    components: tuple[str, ...]
    start: int  # scheduled window [start, end)
    end: int
    announced: int  # minute the notice went out
    title: str

    def notice_days(self) -> float:
        return round((self.start - self.announced) / 1440, 2)


@dataclass(frozen=True)
class Incident:
    id: str
    components: tuple[str, ...]
    regions: tuple[str, ...]
    start: int
    end: int
    severity: str  # minor, major, critical, maintenance
    root_cause: str  # deploy, config, dependency, capacity, network, force_majeure, customer_caused, maintenance_overrun
    title: str
    summary: str
    caused_by: str | None = None  # customer id for customer_caused

    def public(self) -> dict:
        return {
            "id": self.id, "components": list(self.components), "regions": list(self.regions),
            "start": iso(self.start), "end": iso(self.end), "minutes": self.end - self.start,
            "severity": self.severity, "root_cause": self.root_cause, "title": self.title, "summary": self.summary,
        }
