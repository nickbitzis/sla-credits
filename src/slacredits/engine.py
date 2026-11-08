"""The calculation facade: contract-accurate uptime and credits per customer month, cached.

A customer's monthly uptime is the worst covered component in each contract
segment. The credit for a segment is its tier percent (capped) of the
prorated fee. A month with a contract change has two segments, each on its
own terms.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from fractions import Fraction

from slacredits.credits import credit_amount, prorate, tier_pct, to_usd_cents
from slacredits.model import MONTHS, ContractVersion, Customer, days_in_month, iso
from slacredits.synth import Dataset
from slacredits.uptime import ComponentUptime, Exclusion, component_uptime, exclusions_for, segments


class Engine:
    def __init__(self, ds: Dataset, as_of: date | None = None):
        from slacredits.model import DEMO_AS_OF

        self.ds = ds
        self.as_of = as_of or DEMO_AS_OF
        self._cu: dict[tuple, ComponentUptime] = {}
        self._months: dict[tuple[str, str], dict] = {}
        self._culprits = {i.caused_by for i in ds.incidents if i.caused_by}

    def customer(self, cid: str) -> Customer:
        return self.ds.customers[cid]

    def exclusions(self, v: ContractVersion, cid: str, component: str, start: int, end: int) -> list[Exclusion]:
        return exclusions_for(v, cid, component, start, end, self.ds.maintenance, self.ds.incidents)

    def component_uptime(self, v: ContractVersion, cid: str, component: str, start: int, end: int) -> ComponentUptime:
        who = cid if cid in self._culprits else None
        key = (v.quorum, v.measurement, v.window_down_minutes, v.maintenance_notice_days, who, component, start, end)
        if key not in self._cu:
            self._cu[key] = component_uptime(self.ds.probes, v, component, start, end, self.exclusions(v, cid, component, start, end))
        return self._cu[key]

    def month(self, cid: str, month: str, components: list[str] | None = None, claimed_minutes: int | None = None) -> dict:
        """Uptime and credit for one customer month.

        `components` limits the calculation to claimed components. `claimed_minutes`
        (dispute accepted) spreads the customer's figure over the segments by
        eligible minutes and uses it wherever it is higher than measured.
        """
        plain = components is None and claimed_minutes is None
        if plain and (cid, month) in self._months:
            return self._months[(cid, month)]
        if month not in MONTHS:
            raise KeyError(month)
        c = self.customer(cid)
        dim = days_in_month(month)
        fx = self.ds.fx_to_usd[month]
        segs = segments(c, month)
        rows = []
        for sg in segs:
            v = sg.version
            comps = [x for x in v.components if components is None or x in components]
            cus = [self.component_uptime(v, cid, x, sg.start, sg.end) for x in comps]
            fee = prorate(v.monthly_fee, sg.days, dim)
            worst = min(cus, key=lambda u: u.fraction) if cus else None
            rows.append({"version": v.version, "from": iso(sg.start), "to": iso(sg.end), "days": sg.days,
                         "monthly_fee": v.monthly_fee, "prorated_fee": fee, "target": v.target,
                         "definition": v.definition(), "components": [u.to_dict() for u in cus],
                         "_v": v, "_worst": worst})
        if claimed_minutes is not None:
            total_elig = sum(r["_worst"].eligible for r in rows if r["_worst"]) or 1
            for r in rows:
                w = r["_worst"]
                if w:
                    share = round(claimed_minutes * w.eligible / total_elig)
                    if share > w.downtime:
                        r["_worst"] = replace(w, downtime=min(share, w.eligible))
                        r["claimed_minutes_applied"] = share
        credit = 0
        downtime = 0
        worst_frac: Fraction | None = None
        for r in rows:
            w = r.pop("_worst")
            v = r.pop("_v")
            if w is None:
                r.update(worst_component=None, uptime_pct=None, tier_pct=0, credit=0, met=True)
                continue
            pct = tier_pct(v, w.fraction)
            amt = credit_amount(r["prorated_fee"], pct)
            r.update(worst_component=w.component, uptime_pct=w.pct, downtime_minutes=w.downtime,
                     tier_pct=pct, credit=amt, met=w.fraction * 100 >= Fraction(str(v.target)))
            credit += amt
            downtime += w.downtime
            worst_frac = w.fraction if worst_frac is None else min(worst_frac, w.fraction)
        last = segs[-1].version if segs else c.versions[-1]
        out = {
            "customer_id": cid, "month": month, "currency": c.currency, "segment": c.segment,
            "basis": "proactive" if last.proactive else "claim_required", "days_in_month": dim,
            "active": bool(segs), "segments": rows,
            "fee_total": sum(r["prorated_fee"] for r in rows),
            "uptime_pct": round(float(worst_frac * 100), 4) if worst_frac is not None else None,
            "downtime_minutes": downtime, "credit": credit, "fx_to_usd": fx[c.currency],
            "credit_usd_cents": to_usd_cents(credit, c.currency, fx),
            "fee_usd_cents": to_usd_cents(sum(r["prorated_fee"] for r in rows), c.currency, fx),
            "claim_window_days": last.claim_window_days, "covered": sorted({x for s in segs for x in s.version.components}),
        }
        if plain:
            self._months[(cid, month)] = out
        return out

    def year(self, cid: str) -> list[dict]:
        return [self.month(cid, m) for m in MONTHS]

    def all_month(self, month: str) -> list[dict]:
        return [self.month(cid, month) for cid in self.ds.customers]

    def compare(self, month: str, component: str) -> list[dict]:
        """The same month and data under different contract definitions."""
        from slacredits.model import month_bounds

        lo, hi = month_bounds(month)
        base = next(iter(self.ds.customers.values())).versions[0]
        rows = []
        for quorum in (1, 2, 3):
            for measurement, wdm in (("minute", 0), ("window5", 1), ("window5", 3)):
                for notice in (5, 7):
                    v = replace(base, quorum=quorum, measurement=measurement, window_down_minutes=wdm,
                                maintenance_notice_days=notice, components=(component,))
                    u = self.component_uptime(v, "_compare", component, lo, hi)
                    rows.append({"quorum": quorum, "measurement": measurement, "window_down_minutes": wdm,
                                 "maintenance_notice_days": notice, "definition": v.definition(), **u.to_dict()})
        return rows
