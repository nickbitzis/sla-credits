import pytest
from fastapi.testclient import TestClient

from slacredits.api import create_app
from slacredits.engine import Engine
from slacredits.store import Store
from slacredits.synth import generate


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(Engine(generate()), Store(":memory:")))


def test_healthz_and_index(client):
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/")
    assert r.status_code == 200 and "sla-credits" in r.text


def test_metrics(client):
    client.get("/v1/customers")
    body = client.get("/metrics").text
    assert "slacredits_http_requests_total" in body and "slacredits_claims{" in body and "slacredits_accrual_usd_cents" in body


def test_customer_and_uptime(client):
    cs = client.get("/v1/customers").json()
    assert cs["count"] == 300
    cid = cs["customers"][0]["id"]
    c = client.get(f"/v1/customers/{cid}").json()
    assert c["contract_versions"][0]["tiers"]
    up = client.get(f"/v1/customers/{cid}/uptime").json()
    assert len(up["months"]) == 12
    m = client.get(f"/v1/customers/{cid}/uptime/2026-03").json()
    assert m["segments"] and "exclusions" in m


def test_errors_share_shape(client):
    r = client.get("/v1/customers/zzz")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = client.get("/v1/credits?month=2027-01")
    assert r.status_code == 422 and r.json()["error"] == "unknown_month"


def test_credits_and_compare(client):
    cr = client.get("/v1/credits?month=2026-03").json()
    assert cr["credited"] > 250
    cmp_ = client.get("/v1/compare?month=2026-03&component=api").json()["definitions"]
    assert len(cmp_) == 18 and len({r["downtime_minutes"] for r in cmp_}) > 2


def test_claim_lifecycle(client):
    cust = client.get("/v1/credits?month=2026-03").json()["customers"]
    claimed = {c["customer_id"] for c in client.get("/v1/claims?limit=2000").json()["claims"] if c["month"] == "2026-03"}
    target = next(r for r in cust if r["basis"] == "claim_required" and 0 < r["credit_usd_cents"] < 150_000
                  and r["customer_id"] not in claimed)
    cid = target["customer_id"]
    m = client.get(f"/v1/customers/{cid}/uptime/2026-03").json()
    comp = m["segments"][-1]["worst_component"]
    r = client.post("/v1/claims", json={"customer_id": cid, "month": "2026-03", "components": [comp],
                                        "claimed_minutes": m["downtime_minutes"], "submitted_on": "2026-04-06"})
    assert r.status_code == 201, r.text
    cl = r.json()
    assert cl["status"] == "pending_review"
    r = client.post(f"/v1/claims/{cl['id']}/approve", json={"approver": "tester", "role": "support", "on": "2026-04-08"})
    assert r.json()["status"] == "approved"
    r = client.post(f"/v1/claims/{cl['id']}/approve", json={"approver": "tester2", "role": "support"})
    assert r.status_code == 409
    audit = client.get(f"/v1/audit?entity={cl['id']}").json()["entries"]
    assert [a["action"] for a in audit] == ["claim.approved", "claim.submitted"]


def test_claim_validation_errors(client):
    r = client.post("/v1/claims", json={"customer_id": "c001", "month": "2026-03", "components": ["nope"], "claimed_minutes": 5})
    assert r.status_code == 422
    r = client.post("/v1/claims", json={"customer_id": "c001", "month": "bad", "components": ["api"], "claimed_minutes": 5})
    assert r.status_code == 422


def test_dispute_flow(client):
    open_ = client.get("/v1/disputes?status=open").json()["disputes"]
    assert open_
    d = open_[0]
    ev = client.get(f"/v1/disputes/{d['id']}/evidence").json()
    assert ev["probe_timeline"] is not None and ev["contract_terms"]
    r = client.post(f"/v1/disputes/{d['id']}/resolve", json={"decision": "upheld", "resolver": "desk", "notes": "probes stand"})
    assert r.status_code == 200 and r.json()["dispute"]["status"] == "resolved"


def test_finance_exports(client):
    js = client.get("/v1/finance/memos?month=2026-03").json()
    csv_ = client.get("/v1/finance/memos?month=2026-03&format=csv")
    assert csv_.headers["content-type"].startswith("text/csv")
    assert len(csv_.text.strip().splitlines()) == js["totals"]["count"] + 1
    assert client.get("/v1/finance/accrual").json()["total_usd_cents"] >= 0
    ri = client.get("/v1/finance/revenue-impact").json()
    assert [s["segment"] for s in ri["segments"]] == ["smb", "mid_market", "enterprise"]


def test_status_endpoints(client):
    h = client.get("/v1/status/history?component=api&days=30").json()["days"]
    assert len(h) == 30
    hm = client.get("/v1/status/heatmap").json()["components"]
    assert set(hm) == {"api", "dashboard", "export"} and len(hm["api"]) == 365
    assert client.get("/v1/status/history?component=nope").status_code == 422
    assert client.get("/v1/incidents").json()["incidents"]


def test_request_id_header(client):
    r = client.get("/v1/meta", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
