import csv, json
from pathlib import Path
import pytest
from app.engine.pipeline import Engine
from app.engine.policy import PolicyBook, diff_policies, validate
from app.engine.suppliers import preferred_list
from app.policy_update import fallback_patch, apply_patch
from app.seed import load_full_seed
import datetime as dt

ROOT = Path(__file__).resolve().parent.parent
M = load_full_seed(ROOT / "seed")
POL = json.load(open(ROOT / "policies/policy_v5.0.json"))
RAW = list(csv.DictReader(open(ROOT / "seed/pr_batches/sample_pr_batch.csv")))
# rows appended after the worked examples were produced (shift PO numbers by one)
CORE = [r for r in RAW if r["pr_no"] not in ("PR-26-00172", "PR-26-00173", "PR-26-00174", "PR-26-00175")]

def run(raw=CORE, pol=POL):
    res = Engine(M, PolicyBook([pol])).run(raw)
    return {(r["pr_no"], r["pr_line"]): r for r in res.rows}, res

ROWS, RES = run()

@pytest.mark.parametrize("key,route,supplier,value,po", [
    (("PR-26-00019", "1"), "CONTRACT_PO", "S001", 137531, "PO-0011"),
    (("PR-26-00045", "1"), "CONTRACT_PO", "S021", 26829, "PO-0072"),       # PDF-only contract
    (("PR-26-00146", "1"), "SPOT_PO", "S024", 126472, "PO-0001"),          # clubbed split PRs
    (("PR-26-00034", "1"), "CONTRACT_PO", "S007", 32547, "PO-0003"),       # lowest-rate contract with ceiling
    (("PR-26-00035", "1"), "CONTRACT_PO", "S007", 23227, "PO-0003"),       # consolidated PO
    (("PR-26-00088", "1"), "SPOT_PO", "S023", 97955, "PO-0023"),
    (("PR-26-00059", "1"), "EMERGENCY_PO", "S014", 253184, "PO-0024"),
])
def test_po_examples(key, route, supplier, value, po):
    r = ROWS[key]
    assert (r["route"], r["supplier_id"], r["po_id"]) == (route, supplier, po)
    assert abs(float(r["group_value_inr"]) - value) < 1.5

@pytest.mark.parametrize("key,route,sug", [
    (("PR-26-00004", "1"), "RFQ", "S014;S013;S016"),
    (("PR-26-00089", "1"), "RFQ", "S043;S044;S042"),
    (("PR-26-00050", "1"), "RFQ", "S003;S002;S005"),
    (("PR-26-00100", "1"), "RFP", "S020;S017;S019;S018"),
    (("PR-26-00047", "1"), "CAPEX_SOURCING", "S045;S047;S020;S048;S046"),
    (("PR-26-00170", "1"), "NEW_ITEM_SOURCING", ""),
])
def test_sourcing_examples(key, route, sug):
    r = ROWS[key]
    assert (r["route"], r["suggested_supplier_ids"]) == (route, sug)

def test_not_processed_lines():
    rows, _ = run(RAW)
    assert rows[("PR-26-00172", "1")]["route"] == "NOT_PROCESSED"   # quantity 0
    assert rows[("PR-26-00173", "1")]["route"] == "NOT_PROCESSED"   # plant PUN2

def test_blocked_suppliers_never_used():
    bad = {s for s, v in M.suppliers.items() if v["status"] != "Active"}
    for r in ROWS.values():
        assert r["supplier_id"] not in bad
        assert not (set(r["suggested_supplier_ids"].split(";")) & bad)

@pytest.mark.parametrize("item,expected", [("ITM-1048", ["S029", "S030"]), ("ITM-1036", ["S023", "S022", "S024"]), ("ITM-1016", ["S009", "S010"])])
def test_preferred_lists(item, expected):
    pl = preferred_list(M, item, dt.date(2026, 11, 1), 548, POL["ranking"])
    assert [p.supplier_id for p in pl] == expected

def test_amendment_changes_behaviour_without_code():
    txt = "With effect from 1 November 2026: the RFQ limit... The spot PO limit is increased from ₹2,00,000 to ₹3,00,000."
    patch = fallback_patch(txt, POL, ["Safety & PPE"])
    new = apply_patch(POL, patch, "5.1", "2026-11-01")
    assert validate(new) == [] and new["limits"]["spot_po_limit"] == 300000
    assert any(d["key"] == "spot_po_limit" for d in diff_policies(POL, new))
    book = PolicyBook([POL, new])
    res = Engine(M, book).run(CORE)
    assert {d.policy_version for d in res.decisions} == {"5.1"}
    assert sum(1 for r in res.rows if r["route"] == "SPOT_PO") > sum(1 for r in ROWS.values() if r["route"] == "SPOT_PO")

def test_plant_only_rule_is_pure_data():
    new = json.loads(json.dumps(POL)); new["version"] = "5.2"; new["effective_date"] = "2026-01-01"
    new["rules"].insert(0, {"id": "P1", "title": "CHK1 lubricants always RFQ", "when": {"all": [{"field": "plant", "op": "eq", "value": "CHK1"}, {"field": "category", "op": "eq", "value": "Lubricants"}]},
                            "then": {"decision": "SOURCING_REQUEST", "route": "RFQ", "suggest_suppliers": 3}})
    res = Engine(M, PolicyBook([new])).run(CORE)
    for d in res.decisions:
        if d.requirement.plant == "CHK1" and d.requirement.item and M.items[d.requirement.item]["category"] == "Lubricants":
            assert d.route == "RFQ" and d.rule_id == "P1"
