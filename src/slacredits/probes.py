"""Probe results and the rules that turn them into down minutes.

Every component is probed once a minute from 3 regions. A probe result is
stored sparsely: for each component, a dict of minute -> bitmask of the
regions whose probe failed (bit 0 us-east, bit 1 eu-west, bit 2 ap-south).
A minute that is absent means all three probes passed.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field

from slacredits.model import COMPONENTS, REGIONS

REGION_BIT = {r: 1 << i for i, r in enumerate(REGIONS)}


def popcount(mask: int) -> int:
    return bin(mask).count("1")


def regions_of(mask: int) -> list[str]:
    return [r for r in REGIONS if mask & REGION_BIT[r]]


@dataclass
class ProbeData:
    failures: dict[str, dict[int, int]] = field(default_factory=lambda: {c: {} for c in COMPONENTS})
    _sorted: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _quorum_cache: dict[tuple[str, int], list[int]] = field(default_factory=dict, repr=False)

    def fail(self, component: str, region: str, start: int, end: int) -> None:
        bit = REGION_BIT[region]
        f = self.failures[component]
        for m in range(start, end):
            f[m] = f.get(m, 0) | bit
        self._sorted.clear()
        self._quorum_cache.clear()

    def mask(self, component: str, minute: int) -> int:
        return self.failures[component].get(minute, 0)

    def failing_minutes(self, component: str) -> list[int]:
        if component not in self._sorted:
            self._sorted[component] = sorted(self.failures[component])
        return self._sorted[component]

    def down_minutes(self, component: str, quorum: int, start: int, end: int) -> list[int]:
        """Minutes in [start, end) where at least `quorum` regions failed."""
        key = (component, quorum)
        if key not in self._quorum_cache:
            f = self.failures[component]
            self._quorum_cache[key] = [m for m in self.failing_minutes(component) if popcount(f[m]) >= quorum]
        mins = self._quorum_cache[key]
        return mins[bisect.bisect_left(mins, start): bisect.bisect_left(mins, end)]

    def timeline(self, component: str, start: int, end: int) -> list[tuple[int, int, int]]:
        """Runs of identical failure masks in [start, end): (start, end, mask). Passing minutes are skipped."""
        mins = self.failing_minutes(component)
        lo, hi = bisect.bisect_left(mins, start), bisect.bisect_left(mins, end)
        f = self.failures[component]
        runs: list[tuple[int, int, int]] = []
        for m in mins[lo:hi]:
            mk = f[m]
            if runs and runs[-1][1] == m and runs[-1][2] == mk:
                runs[-1] = (runs[-1][0], m + 1, mk)
            else:
                runs.append((m, m + 1, mk))
        return runs

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for c in COMPONENTS:
            h.update(c.encode())
            h.update(json.dumps(sorted(self.failures[c].items())).encode())
        return h.hexdigest()[:16]


def window_down(down: set[int], excluded: set[int], start: int, end: int, threshold: int) -> tuple[int, int]:
    """5 minute window rule over [start, end).

    Windows are aligned to 5 minute boundaries of the minute clock. Excluded
    minutes are neither up nor down. A window is down when its non-excluded
    down minutes reach `threshold`; then all its non-excluded minutes count as
    downtime. Returns (downtime minutes, down windows).
    """
    windows: dict[int, int] = {}
    for m in down:
        if start <= m < end and m not in excluded:
            w = m - (m % 5)
            windows[w] = windows.get(w, 0) + 1
    minutes = 0
    count = 0
    for w, n in windows.items():
        if n >= threshold:
            count += 1
            minutes += sum(1 for m in range(max(w, start), min(w + 5, end)) if m not in excluded)
    return minutes, count
