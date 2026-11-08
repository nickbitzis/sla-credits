"""Credit tiers, caps, proration, and currency. Integer minor units throughout."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from slacredits.model import ContractVersion


def round_half_up(x: Fraction | Decimal | float) -> int:
    if isinstance(x, Fraction):
        x = Decimal(x.numerator) / Decimal(x.denominator)
    return int(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def prorate(monthly_fee: int, days: int, days_in_month: int) -> int:
    """Fee for `days` of a month, by day: fee x days / days_in_month, rounded half up once."""
    return round_half_up(Fraction(monthly_fee * days, days_in_month))


def tier_pct(v: ContractVersion, uptime: Fraction) -> int:
    """Highest tier whose threshold the uptime falls strictly below, then the cap. `uptime` is a fraction of 1."""
    pct = 0
    for t in v.tiers:
        if uptime * 100 < Fraction(str(t.below)):
            pct = max(pct, t.pct)
    return min(pct, v.cap_pct)


def credit_amount(fee: int, pct: int) -> int:
    return round_half_up(Fraction(fee * pct, 100))


def to_usd_cents(amount_minor: int, currency: str, fx: dict[str, float]) -> int:
    return round_half_up(Decimal(amount_minor) * Decimal(str(fx[currency])))


def fmt_money(minor: int, currency: str) -> str:
    sym = {"USD": "$", "EUR": "EUR ", "GBP": "GBP "}[currency]
    return f"{sym}{minor / 100:,.2f}"
