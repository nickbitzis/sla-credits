from collections import Counter

from slacredits.synth import contract_fingerprint, generate


def test_deterministic():
    a, b = generate(), generate()
    assert a.data_fingerprint() == b.data_fingerprint()
    assert [contract_fingerprint(c) for c in a.customers.values()] == [contract_fingerprint(c) for c in b.customers.values()]


def test_different_seed_differs():
    assert generate(1).data_fingerprint() != generate(2).data_fingerprint()


def test_customer_mix(ds):
    seg = Counter(c.segment for c in ds.customers.values())
    assert len(ds.customers) == 300 and seg == {"smb": 150, "mid_market": 105, "enterprise": 45}
    targets = {v.target for c in ds.customers.values() for v in c.versions}
    assert targets == {99.9, 99.95, 99.99}
    assert {v.measurement for c in ds.customers.values() for v in c.versions} == {"minute", "window5"}
    assert all(v.proactive == (c.segment == "enterprise") for c in ds.customers.values() for v in c.versions)


def test_has_amendments_and_currencies(ds):
    assert sum(1 for c in ds.customers.values() if len(c.versions) == 2) >= 20
    assert {c.currency for c in ds.customers.values()} == {"USD", "EUR", "GBP"}


def test_incident_tags(ds):
    causes = {i.root_cause for i in ds.incidents}
    assert {"force_majeure", "customer_caused", "dependency", "maintenance_overrun"} <= causes
    assert sum(1 for i in ds.incidents if i.severity == "critical") == 1
    assert [i.id for i in ds.incidents] == [f"INC-{n:04d}" for n in range(1, len(ds.incidents) + 1)]


def test_maintenance_notice_varies(ds):
    notices = [m.notice_days() for m in ds.maintenance]
    assert min(notices) < 5 and max(notices) >= 7
