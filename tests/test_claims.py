from datetime import timedelta

import pytest
from helpers import MONTH, claim_customer, in_window

from slacredits import claims
from slacredits.model import month_end_date


def worst(mc):
    return [mc["segments"][0]["worst_component"]]


def test_valid_claim_goes_to_review(engine, store):
    cid, mc = claim_customer(engine, max_usd=1_000_000)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window())
    assert cl["status"] == "pending_review"
    assert cl["credit"] == mc["credit"] and cl["measured_minutes"] == mc["downtime_minutes"]


def test_last_day_of_window_accepted(engine, store):
    cid, mc = claim_customer(engine)
    last = claims.deadline(engine, cid, MONTH)
    assert last == month_end_date(MONTH) + timedelta(days=mc["claim_window_days"])
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], last)
    assert cl["status"] != "rejected_invalid"


def test_day_after_window_rejected(engine, store):
    cid, mc = claim_customer(engine)
    late = claims.deadline(engine, cid, MONTH) + timedelta(days=1)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), 10, late)
    assert cl["status"] == "rejected_invalid" and cl["reason"] == "outside_claim_window"


def test_month_not_closed(engine, store):
    cid, mc = claim_customer(engine)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), 10, month_end_date(MONTH))
    assert cl["reason"] == "month_not_closed"


def test_uncovered_component_rejected(engine, store):
    cid = next(c.id for c in engine.ds.customers.values() if c.versions[-1].components == ("api",) and not c.versions[-1].proactive
               and engine.month(c.id, MONTH)["active"])
    cl = claims.submit(engine, store, cid, MONTH, ["export"], 30, in_window())
    assert cl["status"] == "rejected_invalid" and cl["reason"] == "component_not_covered"


def test_uncovered_components_dropped_when_some_covered(engine, store):
    cid = next(c.id for c in engine.ds.customers.values() if c.versions[-1].components == ("api",) and not c.versions[-1].proactive
               and engine.month(c.id, MONTH)["credit"] > 0 and len(c.versions) == 1)
    cl = claims.submit(engine, store, cid, MONTH, ["api", "export"], 0, in_window())
    assert cl["components"] == ["api"]
    assert store.audit_log(cl["id"])[0]["details"]["dropped"] == ["export"]


def test_proactive_contract_needs_no_claim(engine, store):
    cid = next(c.id for c in engine.ds.customers.values() if c.versions[-1].proactive and engine.month(c.id, MONTH)["active"])
    cl = claims.submit(engine, store, cid, MONTH, ["api"], 10, in_window())
    assert cl["reason"] == "proactive_contract"


def test_duplicate_claim_rejected(engine, store):
    cid, mc = claim_customer(engine)
    claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window())
    again = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window(days=6))
    assert again["reason"] == "duplicate"


def test_unknown_customer_404(engine, store):
    with pytest.raises(claims.ClaimError) as e:
        claims.submit(engine, store, "nope", MONTH, ["api"], 1, in_window())
    assert e.value.status == 404


def test_sla_met_auto_rejected(engine, store):
    month = "2026-02"
    cid = next(cid for cid, c in engine.ds.customers.items()
               if engine.month(cid, month)["credit"] == 0 and engine.month(cid, month)["active"] and not c.versions[-1].proactive)
    measured = engine.month(cid, month, components=["api"])["downtime_minutes"]
    cl = claims.submit(engine, store, cid, month, ["api"], measured, in_window(month))
    assert cl["status"] == "rejected" and cl["reason"] == "sla_met"


def test_claim_above_measured_opens_dispute(engine, store):
    cid, mc = claim_customer(engine)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"] + 120, in_window())
    assert cl["status"] == "disputed" and cl["dispute"]["status"] == "open"


def test_small_tolerance_does_not_dispute(engine, store):
    cid, mc = claim_customer(engine)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"] + claims.DISPUTE_TOLERANCE_MIN, in_window())
    assert cl["status"] == "pending_review"


def test_small_credit_single_approval(engine, store):
    cid, mc = claim_customer(engine, max_usd=claims.FINANCE_THRESHOLD_USD_CENTS)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window())
    cl = claims.approve(store, cl["id"], "alice", "support", in_window(days=8))
    assert cl["status"] == "approved" and cl["decided_on"]


def test_large_credit_needs_finance(engine, store):
    cid, mc = claim_customer(engine, min_usd=claims.FINANCE_THRESHOLD_USD_CENTS)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window())
    cl = claims.approve(store, cl["id"], "alice", "support", in_window(days=8))
    assert cl["status"] == "pending_finance"
    with pytest.raises(claims.ClaimError) as e:
        claims.approve(store, cl["id"], "bob", "support", in_window(days=9))
    assert e.value.code == "finance_required"
    with pytest.raises(claims.ClaimError) as e:
        claims.approve(store, cl["id"], "alice", "finance", in_window(days=9))
    assert e.value.code == "same_approver"
    cl = claims.approve(store, cl["id"], "carol", "finance", in_window(days=9))
    assert cl["status"] == "approved" and len(cl["approvals"]) == 2


def test_reject_and_no_double_decision(engine, store):
    cid, mc = claim_customer(engine)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window())
    cl = claims.reject(store, cl["id"], "alice", "customer withdrew", in_window(days=8))
    assert cl["status"] == "rejected"
    with pytest.raises(claims.ClaimError):
        claims.approve(store, cl["id"], "bob", "support", in_window(days=9))


def test_every_transition_audited(engine, store):
    cid, mc = claim_customer(engine, max_usd=claims.FINANCE_THRESHOLD_USD_CENTS)
    cl = claims.submit(engine, store, cid, MONTH, worst(mc), mc["downtime_minutes"], in_window())
    claims.approve(store, cl["id"], "alice", "support", in_window(days=8))
    actions = [a["action"] for a in store.audit_log(cl["id"])]
    assert actions == ["claim.approved", "claim.submitted"]


def test_audit_log_append_only(store):
    store.audit("x", "test", "e1")
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError):
        store.x("DELETE FROM audit")
    with pytest.raises(sqlite3.DatabaseError):
        store.x("UPDATE audit SET actor = 'y'")
