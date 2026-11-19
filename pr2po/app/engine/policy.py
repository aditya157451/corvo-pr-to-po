"""Policy-as-data: a policy is a JSON document. This module contains NO business rules,
only a generic interpreter (conditions -> outcome), a validator and a version book.

Policy document shape (see policies/policy_v5.0.json):
  version, effective_date, params{}, limits{}, ranking{}, rules[ {id,title,when,then} ], fallback{}
A rule's `when` is a condition tree: {"all":[...]} | {"any":[...]} | {"not": cond} |
{"field":..,"op":..,"value":.. | "ref":"limits.x"}.  Rules are evaluated in list order,
the first match decides.
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- schema
# Fields a rule may test. All are computed per requirement by the pipeline.
FIELD_CATALOG: dict[str, dict[str, str]] = {
    "is_new_item": {"type": "bool", "help": "Line matched no item in the item master"},
    "item_code": {"type": "text", "help": "Resolved item code"},
    "item_type": {"type": "text", "help": "Material / Service / Capex"},
    "category": {"type": "text", "help": "Item category, e.g. Safety & PPE"},
    "uom": {"type": "text", "help": "Item unit of measure"},
    "plant": {"type": "text", "help": "Plant code, e.g. PUN1 or CHK1"},
    "value": {"type": "number", "help": "Requirement value in INR (policy 3.1)"},
    "quantity": {"type": "number", "help": "Total requirement quantity"},
    "line_count": {"type": "number", "help": "Number of PR lines clubbed in the requirement"},
    "is_emergency": {"type": "bool", "help": "Any line marked Emergency"},
    "contract_usable": {"type": "bool", "help": "A contract applies and has ceiling left (4.1-4.3)"},
    "contract_applicable": {"type": "bool", "help": "A contract covers the item on the requirement date"},
    "contract_count": {"type": "number", "help": "Number of applicable contracts"},
    "days_since_active_purchase": {"type": "number", "help": "Days since the item was last bought from an Active supplier (empty if never)"},
    "days_since_any_purchase": {"type": "number", "help": "Days since the item was last bought from any supplier"},
    "preferred_supplier_count": {"type": "number", "help": "Suppliers on the item's preferred list"},
    "lead_time_days": {"type": "number", "help": "Earliest need-by date minus requirement date, in days"},
    "department": {"type": "list", "help": "Departments of the lines in the requirement"},
}
OPS = ["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "between", "contains", "exists"]
DECISIONS = ["PO", "SOURCING_REQUEST"]
SUPPLIER_MODES = ["contract", "preferred", "none"]
PRICE_MODES = ["contract_rate", "reference_price", "none"]


class PolicyError(ValueError):
    pass


# --------------------------------------------------------------------------- lookup
def get_path(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise PolicyError(f"unknown reference '{path}'")
        cur = cur[part]
    return cur


def _resolve(cond: dict, policy: dict) -> Any:
    if "ref" in cond:
        return get_path(policy, cond["ref"])
    return cond.get("value")


def _cmp_leaf(cond: dict, ctx: dict, policy: dict) -> bool:
    field = cond["field"]
    op = cond["op"]
    actual = ctx.get(field)
    expected = _resolve(cond, policy)
    if op == "exists":
        present = actual not in (None, "", [])
        return present == bool(expected if expected is not None else True)
    if actual is None:
        return False  # unknown never satisfies a comparison
    if isinstance(actual, list):  # list-valued fields (department)
        if op in ("contains", "eq", "in"):
            exp = expected if isinstance(expected, list) else [expected]
            return any(e in actual for e in exp)
        if op in ("ne", "not_in"):
            exp = expected if isinstance(expected, list) else [expected]
            return not any(e in actual for e in exp)
        return False
    if op == "eq":
        return _norm(actual) == _norm(expected)
    if op == "ne":
        return _norm(actual) != _norm(expected)
    if op == "in":
        return _norm(actual) in [_norm(e) for e in expected]
    if op == "not_in":
        return _norm(actual) not in [_norm(e) for e in expected]
    if op == "contains":
        return str(expected).lower() in str(actual).lower()
    if op == "between":
        lo, hi = expected
        return lo <= actual <= hi
    if op in ("lt", "lte", "gt", "gte"):
        a, e = float(actual), float(expected)
        return {"lt": a < e, "lte": a <= e, "gt": a > e, "gte": a >= e}[op]
    raise PolicyError(f"unknown operator '{op}'")


def _norm(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def evaluate(cond: dict, ctx: dict, policy: dict) -> bool:
    """Evaluate a condition tree against a requirement context."""
    if not cond:
        return True
    if "all" in cond:
        return all(evaluate(c, ctx, policy) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, ctx, policy) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], ctx, policy)
    return _cmp_leaf(cond, ctx, policy)


def explain_condition(cond: dict, policy: dict) -> str:
    """Plain-language rendering of a condition tree."""
    if not cond:
        return "always"
    if "all" in cond:
        parts = [explain_condition(c, policy) for c in cond["all"]]
        return " AND ".join(f"({p})" if (" OR " in p) else p for p in parts) or "always"
    if "any" in cond:
        return " OR ".join(explain_condition(c, policy) for c in cond["any"])
    if "not" in cond:
        return f"NOT ({explain_condition(cond['not'], policy)})"
    sym = {"eq": "is", "ne": "is not", "lt": "<", "lte": "≤", "gt": ">", "gte": "≥", "in": "is one of",
           "not_in": "is not one of", "between": "is between", "contains": "contains", "exists": "is present"}
    exp = cond.get("value")
    if "ref" in cond:
        try:
            exp = f"{get_path(policy, cond['ref'])} ({cond['ref']})"
        except PolicyError:
            exp = f"[{cond['ref']}?]"
    if isinstance(exp, bool):
        exp = "yes" if exp else "no"
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and abs(exp) >= 1000:
        exp = f"₹{exp:,.0f}" if cond["field"] == "value" else f"{exp:,}"
    if cond["op"] == "exists":
        return f"{cond['field']} {'is present' if cond.get('value', True) else 'is empty'}"
    return f"{cond['field']} {sym.get(cond['op'], cond['op'])} {exp}"


def explain_outcome(then: dict) -> str:
    if then.get("decision") == "PO":
        who = "contract supplier at contract rate" if then.get("supplier") == "contract" else \
            f"preferred supplier #{then.get('supplier_rank', 1)} at its reference price"
        return f"{then.get('route')}: place a PO with the {who}"
    n = then.get("suggest_suppliers", 0)
    extra = f", suggest {n} supplier(s)" if n else ", no supplier suggestions"
    ap = f", needs {then['requires_approval']} approval" if then.get("requires_approval") else ""
    return f"{then.get('route')}: raise a sourcing request{extra}{ap}"


# --------------------------------------------------------------------------- validation
def validate(policy: dict) -> list[str]:
    """Return a list of human-readable problems (empty list = valid)."""
    errs: list[str] = []
    for k in ("version", "effective_date", "rules"):
        if k not in policy:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    try:
        dt.date.fromisoformat(policy["effective_date"])
    except Exception:
        errs.append(f"effective_date '{policy['effective_date']}' is not YYYY-MM-DD")
    seen = set()
    for i, r in enumerate(policy["rules"]):
        rid = r.get("id", f"#{i}")
        if rid in seen:
            errs.append(f"duplicate rule id {rid}")
        seen.add(rid)
        errs += [f"rule {rid}: {e}" for e in _validate_cond(r.get("when", {}), policy)]
        errs += [f"rule {rid}: {e}" for e in _validate_then(r.get("then", {}))]
    if "fallback" in policy:
        errs += [f"fallback: {e}" for e in _validate_then(policy["fallback"].get("then", {}))]
    return errs


def _validate_cond(cond: dict, policy: dict) -> list[str]:
    if not cond:
        return []
    for key in ("all", "any"):
        if key in cond:
            out: list[str] = []
            for c in cond[key]:
                out += _validate_cond(c, policy)
            return out
    if "not" in cond:
        return _validate_cond(cond["not"], policy)
    errs = []
    if cond.get("field") not in FIELD_CATALOG:
        errs.append(f"unknown field '{cond.get('field')}'")
    if cond.get("op") not in OPS:
        errs.append(f"unknown operator '{cond.get('op')}'")
    if "ref" in cond:
        try:
            get_path(policy, cond["ref"])
        except PolicyError:
            errs.append(f"unknown reference '{cond['ref']}'")
    elif "value" not in cond and cond.get("op") != "exists":
        errs.append("condition needs 'value' or 'ref'")
    return errs


def _validate_then(then: dict) -> list[str]:
    errs = []
    if then.get("decision") not in DECISIONS:
        errs.append(f"decision must be one of {DECISIONS}")
    if not then.get("route"):
        errs.append("route is required")
    if then.get("decision") == "PO" and then.get("supplier", "preferred") not in SUPPLIER_MODES:
        errs.append(f"supplier must be one of {SUPPLIER_MODES}")
    return errs


# --------------------------------------------------------------------------- rule selection
@dataclass
class RuleMatch:
    rule_id: str
    title: str
    then: dict
    skipped: list[str]  # rules that matched but could not be applied (e.g. no supplier available)


def select_rule(policy: dict, ctx: dict, can_apply) -> RuleMatch:
    """First rule whose condition holds AND whose outcome is feasible (`can_apply(then)->str|None`
    returns a reason when infeasible, e.g. a PO rule but no supplier available)."""
    skipped: list[str] = []
    for r in policy["rules"]:
        if evaluate(r.get("when", {}), ctx, policy):
            why_not = can_apply(r["then"])
            if why_not:
                skipped.append(f"{r['id']} matched but not applicable: {why_not}")
                continue
            return RuleMatch(r["id"], r.get("title", ""), r["then"], skipped)
    fb = policy.get("fallback") or {"id": "fallback", "title": "No rule matched",
                                    "then": {"decision": "SOURCING_REQUEST", "route": "RFQ", "suggest_suppliers": 3}}
    return RuleMatch(fb.get("id", "fallback"), fb.get("title", "No rule matched"), fb["then"], skipped)


# --------------------------------------------------------------------------- version book
class PolicyBook:
    """All approved policy versions. `at(date)` returns the version in force on that date."""

    def __init__(self, versions: list[dict]):
        self.versions = sorted(copy.deepcopy(versions), key=lambda v: (v["effective_date"], str(v["version"])))
        if not self.versions:
            raise PolicyError("no active policy versions")

    def at(self, d: dt.date) -> dict:
        chosen = None
        for v in self.versions:
            if dt.date.fromisoformat(v["effective_date"]) <= d:
                chosen = v
        return chosen or self.versions[0]

    def pinned(self, version: str) -> "PolicyBook":
        for v in self.versions:
            if str(v["version"]) == str(version):
                pb = PolicyBook([dict(v, effective_date="1900-01-01")])
                return pb
        raise PolicyError(f"unknown version {version}")


# --------------------------------------------------------------------------- diffing
def diff_policies(old: dict, new: dict) -> list[dict]:
    """Structured, plain-language diff between two policy documents."""
    out: list[dict] = []

    def scalar_section(name: str, label: str):
        o, n = old.get(name, {}), new.get(name, {})
        for k in sorted(set(o) | set(n)):
            if o.get(k) != n.get(k):
                kind = "added" if k not in o else "removed" if k not in n else "changed"
                out.append({"kind": kind, "section": name, "key": k,
                            "text": _scalar_text(label, k, o.get(k), n.get(k), kind), "old": o.get(k), "new": n.get(k)})

    scalar_section("limits", "Limit")
    scalar_section("params", "Setting")
    if old.get("ranking") != new.get("ranking"):
        out.append({"kind": "changed", "section": "ranking", "key": "ranking",
                    "text": "Supplier ranking configuration changed", "old": old.get("ranking"), "new": new.get("ranking")})
    if old.get("effective_date") != new.get("effective_date"):
        out.append({"kind": "changed", "section": "meta", "key": "effective_date",
                    "text": f"Effective date {old.get('effective_date')} → {new.get('effective_date')}",
                    "old": old.get("effective_date"), "new": new.get("effective_date")})
    orules = {r["id"]: r for r in old.get("rules", [])}
    nrules = {r["id"]: r for r in new.get("rules", [])}
    for rid, r in nrules.items():
        if rid not in orules:
            pos = [x["id"] for x in new["rules"]].index(rid) + 1
            out.append({"kind": "added", "section": "rules", "key": rid,
                        "text": f"NEW rule {rid} (position {pos}): when {explain_condition(r.get('when', {}), new)} → {explain_outcome(r['then'])}",
                        "old": None, "new": r})
        elif orules[rid] != r:
            ow, nw = explain_condition(orules[rid].get("when", {}), old), explain_condition(r.get("when", {}), new)
            parts = []
            if ow != nw:
                parts.append(f"condition: '{ow}' → '{nw}'")
            if orules[rid].get("then") != r.get("then"):
                parts.append(f"outcome: '{explain_outcome(orules[rid]['then'])}' → '{explain_outcome(r['then'])}'")
            if not parts:
                parts.append("wording changed")
            out.append({"kind": "changed", "section": "rules", "key": rid,
                        "text": f"Rule {rid} changed: " + "; ".join(parts), "old": orules[rid], "new": r})
    for rid, r in orules.items():
        if rid not in nrules:
            out.append({"kind": "removed", "section": "rules", "key": rid,
                        "text": f"Rule {rid} REMOVED ({r.get('title', '')})", "old": r, "new": None})
    oo, no = [r["id"] for r in old.get("rules", []) if r["id"] in nrules], [r["id"] for r in new.get("rules", []) if r["id"] in orules]
    if oo != no:
        out.append({"kind": "changed", "section": "rules", "key": "order",
                    "text": f"Rule order changed: {' → '.join(oo)}  ⇒  {' → '.join(no)}", "old": oo, "new": no})
    return out


def _scalar_text(label: str, key: str, o: Any, n: Any, kind: str) -> str:
    def f(x):
        if isinstance(x, (int, float)) and not isinstance(x, bool) and abs(x) >= 1000:
            return f"₹{x:,.0f}"
        return str(x)
    if kind == "added":
        return f"{label} '{key}' added: {f(n)}"
    if kind == "removed":
        return f"{label} '{key}' removed (was {f(o)})"
    return f"{label} '{key}': {f(o)} → {f(n)}"
