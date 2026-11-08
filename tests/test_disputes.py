import pytest
from helpers import MONTH, claim_customer, in_window

from slacredits import claims
from slacredits.credits import credit_amount


def open_dispute(engine, store, extra=180):
    cid, mc = claim_customer(engine)
    comps = [mc["segments"][0]["worst_component"]]
    cl = claims.submit(engine, store, cid, MONTH, comps, mc["downtime_minutes"] + extra, in_window())
    return cl["dispute"]["id"], cid, mc, cl


def test_evidence_pack_contents(engine, store):
    did, cid, mc, cl = open_dispute(engine, store)
    ev = claims.evidence_pack(engine, store, did)
    for key in ("dispute", "claim", "contract_terms", "measured", "probe_timeline", "incidents", "exclusions", "data_fingerprint", "audit"):
        assert key in ev
    assert ev["difference_minutes"] == 180
    assert ev["measured"]["downtime_minutes"] == mc["downtime_minutes"]
    assert any(i["root_cause"] == "dependency" and i["severity"] == "critical" for i in ev["incidents"])
    assert ev["probe_timeline"] and all("regions_failing" in t and "meets_quorum" in t for t in ev["probe_timeline"])
    assert {a["action"] for a in ev["audit"]} >= {"claim.submitted", "dispute.opened"}


def test_evidence_timeline_matches_measured_minutes_for_minute_contracts(engine, store):
    did, cid, mc, cl = open_dispute(engine, store)
    v = engine.customer(cid).versions[-1]
    ev = claims.evidence_pack(engine, store, did)
    if v.measurement == "minute":
        counted = sum(t["minutes"] for t in ev["probe_timeline"] if t["meets_quorum"] and not t["excluded_by"])
        assert counted == ev["measured"]["downtime_minutes"]


def test_evidence_lists_exclusions_with_notice(engine, store):
    did, *_ = open_dispute(engine, store)
    ev = claims.evidence_pack(engine, store, did)
    for e in ev["exclusions"]:
        if e["kind"] == "maintenance":
            assert "notice_days" in e["evidence"] and "required_days" in e["evidence"]


def test_resolve_upheld_keeps_measured_credit(engine, store):
    did, cid, mc, cl = open_dispute(engine, store)
    out = claims.resolve(engine, store, did, "upheld", "desk", "probes stand", in_window(days=20))
    assert out["dispute"]["decision"] == "upheld"
    assert out["claim"]["status"] == "approved" and out["claim"]["credit"] == mc["credit"]


def test_resolve_goodwill_adds_within_cap(engine, store):
    did, cid, mc, cl = open_dispute(engine, store)
    out = claims.resolve(engine, store, did, "goodwill", "desk", "goodwill", in_window(days=20), goodwill_pct=5)
    cap = credit_amount(mc["fee_total"], engine.customer(cid).versions[-1].cap_pct)
    assert out["claim"]["credit"] == min(cap, mc["credit"] + credit_amount(mc["fee_total"], 5))


def test_resolve_accepted_uses_customer_figure(engine, store):
    did, cid, mc, cl = open_dispute(engine, store, extra=3000)
    out = claims.resolve(engine, store, did, "accepted", "desk", "customer logs", in_window(days=20))
    assert out["claim"]["credit"] >= mc["credit"]
    assert out["dispute"]["final_credit"] == out["claim"]["credit"]


def test_cannot_resolve_twice(engine, store):
    did, *_ = open_dispute(engine, store)
    claims.resolve(engine, store, did, "upheld", "desk", "ok", in_window(days=20))
    with pytest.raises(claims.ClaimError) as e:
        claims.resolve(engine, store, did, "upheld", "desk", "again", in_window(days=21))
    assert e.value.status == 409


def test_bad_goodwill_rejected(engine, store):
    did, *_ = open_dispute(engine, store)
    with pytest.raises(claims.ClaimError):
        claims.resolve(engine, store, did, "goodwill", "desk", "x", in_window(days=20), goodwill_pct=0)
