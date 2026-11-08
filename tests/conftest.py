from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from slacredits.engine import Engine
from slacredits.model import ContractVersion
from slacredits.seed import seed_history
from slacredits.store import Store
from slacredits.synth import TIERS, generate

BASE = ContractVersion(
    contract_id="K-test", version=1, effective=date(2024, 1, 1), monthly_fee=1_000_000, currency="USD", target=99.9,
    measurement="minute", window_down_minutes=0, quorum=2, components=("api",), tiers=TIERS[99.9], cap_pct=50,
    claim_window_days=30, maintenance_notice_days=5, proactive=False,
)


def version(**kw) -> ContractVersion:
    if "target" in kw and "tiers" not in kw:
        kw["tiers"] = TIERS[kw["target"]]
    return replace(BASE, **kw)


@pytest.fixture(scope="session")
def ds():
    return generate()


@pytest.fixture(scope="session")
def engine(ds):
    return Engine(ds)


@pytest.fixture(scope="session")
def seeded(engine):
    store = Store(":memory:")
    seed_history(engine, store)
    return store


@pytest.fixture
def store():
    return Store(":memory:")
