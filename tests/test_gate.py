import json
from dataclasses import replace
from pathlib import Path

from slacredits import credits as credits_mod
from slacredits import engine as engine_mod
from slacredits.engine import Engine
from slacredits.gate import check, snapshot, write_golden
from slacredits.synth import generate

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "credits-2026-03.json"


def test_committed_golden_matches():
    res = check(Engine(generate()), json.loads(GOLDEN.read_text()))
    assert res["ok"] and not res["violations"] and not res["data_changed"], res["violations"][:3]
    assert res["checked"] == 300


def test_code_change_that_moves_money_fails(monkeypatch):
    golden = json.loads(GOLDEN.read_text())
    monkeypatch.setattr(engine_mod, "credit_amount", lambda fee, pct: credits_mod.credit_amount(fee, pct) + (1 if pct else 0))
    res = check(Engine(generate()), golden)
    assert not res["ok"] and res["violations"]
    assert all(v["why"] == "credit changed with no contract or data change" for v in res["violations"])


def test_contract_change_is_explained():
    e = Engine(generate())
    golden = snapshot(e)
    cid = next(cid for cid, v in golden["customers"].items() if v["credit"] > 0)
    c = e.ds.customers[cid]
    c.versions[-1] = replace(c.versions[-1], monthly_fee=c.versions[-1].monthly_fee * 2)
    res = check(Engine(e.ds), golden)
    assert res["ok"] and any(x["customer_id"] == cid and x["why"] == "contract changed" for x in res["explained"])


def test_data_change_is_explained():
    e = Engine(generate())
    golden = snapshot(e)
    from slacredits.model import month_bounds

    lo, _ = month_bounds("2026-03")
    for r in ("us-east", "eu-west", "ap-south"):
        e.ds.probes.fail("api", r, lo + 5000, lo + 5300)
    res = check(Engine(e.ds), golden)
    assert res["data_changed"] and res["ok"] and res["explained"]


def test_write_golden_roundtrip(tmp_path):
    e = Engine(generate())
    p = tmp_path / "g.json"
    snap = write_golden(e, p)
    assert json.loads(p.read_text()) == json.loads(json.dumps(snap))
    assert check(e, snap)["ok"]
