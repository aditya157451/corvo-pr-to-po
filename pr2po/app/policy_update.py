"""Policy amendments as data.

An amendment document (PDF/DOCX/text) or a chat instruction is turned into a *patch*: a short list of
operations on the current policy JSON. `apply_patch` is generic, so a new kind of rule (e.g. one that only
applies to one plant) is just an `add_rule` operation with conditions on existing fields: no code change.

Reading the document: LLM when ANTHROPIC_API_KEY is set (primary), otherwise a deterministic fallback
that understands the common phrasings (limits, windows, plant/category scoped rules).
"""
from __future__ import annotations

import copy
import datetime as dt
import io
import json
import re

from . import llm
from .engine import policy as P

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


class PatchError(ValueError):
    pass


# --------------------------------------------------------------------------- text extraction
def extract_text(data: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if name.endswith(".docx"):
        import docx
        doc = docx.Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- apply a patch
def next_version(base_version: str, existing: set[str]) -> str:
    m = re.match(r"^(\d+)\.(\d+)$", str(base_version))
    major, minor = (int(m.group(1)), int(m.group(2))) if m else (int(float(base_version)) if str(base_version).replace('.', '', 1).isdigit() else 1, 0)
    while True:
        minor += 1
        v = f"{major}.{minor}"
        if v not in existing:
            return v


def _index(rules: list[dict], rid: str) -> int:
    for i, r in enumerate(rules):
        if r["id"] == rid:
            return i
    raise PatchError(f"rule '{rid}' does not exist")


def apply_patch(base: dict, patch: dict, new_version: str, effective_date: str) -> dict:
    doc = copy.deepcopy(base)
    doc.pop("_id", None)
    doc["version"], doc["effective_date"] = new_version, effective_date
    doc.setdefault("limits", {})
    doc.setdefault("params", {})
    rules = doc["rules"]
    for n, op in enumerate(patch.get("operations", []), 1):
        kind = op.get("op")
        try:
            if kind == "set_limit":
                doc["limits"][op["key"]] = _number(op["value"])
            elif kind == "set_param":
                doc["params"][op["key"]] = _number(op["value"])
            elif kind == "set_ranking":
                cur = doc.setdefault("ranking", {})
                parts = op["path"].split(".")
                for k in parts[:-1]:
                    cur = cur.setdefault(k, {})
                cur[parts[-1]] = op["value"]
            elif kind == "add_rule":
                rule = copy.deepcopy(op["rule"])
                rule.setdefault("title", rule["id"])
                if any(r["id"] == rule["id"] for r in rules):
                    raise PatchError(f"rule id '{rule['id']}' already exists")
                if op.get("after"):
                    pos = _index(rules, op["after"]) + 1
                elif op.get("before"):
                    pos = _index(rules, op["before"])
                elif op.get("position") == "first":
                    pos = 0
                else:
                    pos = len(rules)
                rules.insert(pos, rule)
            elif kind == "update_rule":
                r = rules[_index(rules, op["id"])]
                ch = op.get("changes", {})
                if "title" in ch:
                    r["title"] = ch["title"]
                if "when" in ch:
                    r["when"] = ch["when"]
                if "then" in ch:
                    for k, v in ch["then"].items():
                        if v is None:
                            r["then"].pop(k, None)
                        else:
                            r["then"][k] = v
            elif kind == "remove_rule":
                rules.pop(_index(rules, op["id"]))
            elif kind == "move_rule":
                r = rules.pop(_index(rules, op["id"]))
                pos = _index(rules, op["after"]) + 1 if op.get("after") else _index(rules, op["before"])
                rules.insert(pos, r)
            else:
                raise PatchError(f"unknown operation '{kind}'")
        except KeyError as e:
            raise PatchError(f"operation {n} ({kind}) is missing {e}")
    return doc


def _number(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    s = str(v).replace(",", "").replace("₹", "").strip()
    return float(s) if "." in s else int(s)


# --------------------------------------------------------------------------- LLM extraction
LLM_SYSTEM = """You maintain a procurement policy that is stored as JSON. You are given the CURRENT policy JSON and a document
(an amendment, a full replacement policy, or a chat instruction). Output ONLY a JSON object describing the change as operations
on the current policy. Do not restate unchanged parts.

Output shape:
{"summary": "<2-3 plain sentences>",
 "effective_date": "YYYY-MM-DD" | null,        // the date the document says the change applies from
 "assumptions": ["..."],                       // anything ambiguous you had to decide
 "operations": [ ... ]}

Operations (use ONLY these):
 {"op":"set_limit","key":"spot_po_limit","value":300000}              // limits are INR numbers; new keys are allowed
 {"op":"set_param","key":"club_created_within_days","value":10}
 {"op":"set_ranking","path":"weights.contract","value":0.3}
 {"op":"add_rule","after":"5.2" | "before":"5.4" | "position":"first"|"last","rule":{"id":"5.3a","title":"...","when":<condition>,"then":<outcome>}}
 {"op":"update_rule","id":"5.4","changes":{"title":"...","when":<condition>,"then":{...partial outcome keys...}}}
 {"op":"remove_rule","id":"5.3"}
 {"op":"move_rule","id":"5.3a","after":"5.1"}

<condition> is {"all":[...]} | {"any":[...]} | {"not":<condition>} | {"field":F,"op":OP,"value":V} | {"field":F,"op":OP,"ref":"limits.spot_po_limit"}.
Fields: FIELDS. Operators: eq, ne, lt, lte, gt, gte, in, not_in, between, contains, exists.
Categories are exactly as in the item master (e.g. "Safety & PPE"); plants are PUN1 and CHK1; item_type is Material, Service or Capex.
<outcome> = {"decision":"PO"|"SOURCING_REQUEST","route":"CONTRACT_PO|EMERGENCY_PO|SPOT_PO|RFQ|RFP|CAPEX_SOURCING|NEW_ITEM_SOURCING",
 "supplier":"contract"|"preferred","supplier_rank":1,"price":"contract_rate"|"reference_price","suggest_suppliers":3,"requires_approval":"capex"}.

Guidance:
- Rules are evaluated top to bottom and the first match decides, so place a more specific rule BEFORE the general rule it overrides.
- Prefer changing a limit with set_limit when the document changes a number that an existing rule already references.
- A restriction that applies only to some plant/category/item_type/value band is a NEW rule with conditions on those fields placed before the general rule; reuse the outcome shape of the nearest existing rule.
- Rule ids must be unique strings; invent ids like "5.3a" next to the rule they relate to.
- Keep all existing behaviour that the document does not change.
- If the document gives no effective date, use null."""


def llm_patch(text: str, base: dict, instruction: str = "") -> dict:
    clean = {k: v for k, v in base.items() if k not in ("_id",)}
    system = LLM_SYSTEM.replace("FIELDS", ", ".join(f"{k} ({v['type']})" for k, v in P.FIELD_CATALOG.items()))
    user = f"CURRENT POLICY JSON:\n{json.dumps(clean, indent=1)}\n\nDOCUMENT:\n{text[:30000]}\n\n" + (f"EXTRA INSTRUCTION FROM USER: {instruction}\n" if instruction else "")
    return llm.complete_json(system, user, max_tokens=6000, retries=1)


# --------------------------------------------------------------------------- deterministic fallback
def parse_amount(s: str) -> float | None:
    s = s.lower().replace("₹", " ").replace("inr", " ").replace("rs.", " ").replace("rs", " ")
    m = re.search(r"([\d][\d,]*(?:\.\d+)?)\s*(crore|cr|lakh|lakhs|lac|lacs|l\b|k\b)?", s)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").strip()
    mult = {"crore": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5, "k": 1e3}.get(unit, 1)
    return n * mult


def parse_date_text(text: str) -> str | None:
    t = text.lower()
    m = re.search(r"(?:effective|with effect|w\.e\.f\.?|applies|applicable|from)[^.\n]{0,40}?(\d{4}-\d{2}-\d{2})", t)
    if m:
        return m.group(1)
    for pat in (r"(?:effective|with effect from|w\.e\.f\.?|applies from|applicable from|from)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})[a-z]*,?\s+(\d{4})",
                r"(?:effective|with effect from|w\.e\.f\.?|applies from|applicable from|from)\s+([a-z]{3,9})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"):
        for m in re.finditer(pat, t):
            a, b, y = m.groups()
            day, mon = (a, b) if a.isdigit() else (b, a)
            mon_n = next((v for k, v in MONTHS.items() if k.startswith(mon[:3])), None)
            if mon_n:
                try:
                    return dt.date(int(y), mon_n, int(day)).isoformat()
                except ValueError:
                    pass
    m = re.search(r"(?:effective|with effect from|w\.e\.f\.?|from)\s+(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", t)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    return None


AMOUNT_RE = re.compile(r"(?:₹|inr|rs\.?)\s*[\d][\d,]*(?:\.\d+)?\s*(?:crore|cr|lakhs?|lacs?|k)?\b|\b[\d][\d,]*(?:\.\d+)?\s*(?:crore|lakhs?|lacs?)\b")
PLANT_WORDS = {"pun1": "PUN1", "bhosari": "PUN1", "pune plant": "PUN1", "chk1": "CHK1", "chakan": "CHK1"}


def fallback_patch(text: str, base: dict, categories: list[str]) -> dict:
    ops: list[dict] = []
    assumptions: list[str] = []
    sentences = [s.strip() for s in re.split(r"(?<=[.;\n])\s+|\n+", text) if len(s.strip()) > 8]
    limit_keys = {"spot": "spot_po_limit", "emergency": "emergency_po_limit", "rfq": "rfq_limit", "rfp": "rfq_limit", "formal": "rfq_limit"}
    cat_l = {c.lower(): c for c in categories}
    rules = {r["id"]: r for r in base["rules"]}
    n_new = 0

    def find_scope(s: str):
        sl = s.lower()
        plant = next((v for k, v in PLANT_WORDS.items() if k in sl), None)
        cat = next((orig for low, orig in cat_l.items() if low in sl), None)
        return plant, cat

    for s in sentences:
        sl = s.lower()
        amts = [a for a in (parse_amount(x) for x in AMOUNT_RE.findall(sl)) if a]
        amt = (amts[-1] if (len(amts) > 1 and " to " in sl) else amts[0]) if amts else None
        plant, cat = find_scope(s)
        # ---- limits
        kind = next((k for k in ("emergency", "spot", "rfp", "rfq", "formal") if k in sl), None)
        if kind and amt and re.search(r"limit|threshold|up to|below|under|ceiling|cap|not exceed|at or above|exceed", sl):
            key = limit_keys[kind]
            if plant or cat:
                base_rule = {"emergency": "5.3", "spot": "5.4"}.get(kind)
                if base_rule and base_rule in rules:
                    n_new += 1
                    rid = f"{base_rule}{chr(96 + n_new)}"
                    when = copy.deepcopy(rules[base_rule]["when"])
                    scope = []
                    scope_txt = []
                    if plant:
                        scope.append({"field": "plant", "op": "eq", "value": plant}); scope_txt.append(plant)
                    if cat:
                        scope.append({"field": "category", "op": "eq", "value": cat}); scope_txt.append(cat)
                    for c in when["all"]:
                        if c.get("field") == "value":
                            c.pop("ref", None)
                            c["value"] = amt
                    when["all"] = scope + when["all"]
                    ops.append({"op": "add_rule", "before": base_rule, "rule": {"id": rid, "title": f"{rules[base_rule]['title']} (only {', '.join(scope_txt)}: limit ₹{amt:,.0f})", "when": when, "then": copy.deepcopy(rules[base_rule]["then"])}})
                    continue
            elif key in base.get("limits", {}):
                ops.append({"op": "set_limit", "key": key, "value": amt})
                continue
        # ---- scoped "must go to RFQ/RFP/sourcing" rules
        if (plant or cat) and re.search(r"(must|always|shall|all).{0,60}(rfq|rfp|quote|quotation|sourcing)", sl) and not amt:
            route = "RFP" if "rfp" in sl else "RFQ"
            n_new += 1
            conds = []
            scope_txt = []
            if plant:
                conds.append({"field": "plant", "op": "eq", "value": plant}); scope_txt.append(plant)
            if cat:
                conds.append({"field": "category", "op": "eq", "value": cat}); scope_txt.append(cat)
            ops.append({"op": "add_rule", "after": "5.1" if "5.1" in rules else None, "position": None if "5.1" in rules else "first",
                        "rule": {"id": f"X.{n_new}", "title": f"{', '.join(scope_txt)}: always {route} (before contracts and value limits)", "when": {"all": conds},
                                 "then": {"decision": "SOURCING_REQUEST", "route": route, "suggest_suppliers": 5 if route == "RFP" else 3}}})
            assumptions.append(f"'{s[:80]}': placed right after the Capex rule, i.e. it overrides contracts and value limits for that scope.")
            continue
        # ---- windows / counts
        m = re.search(r"(\d+)\s*(?:calendar\s*)?days", sl)
        if m and re.search(r"club|same requirement|split", sl) and re.search(r"creat|raised|raise", sl) and "need" not in sl[:sl.find(m.group(0))][-30:]:
            ops.append({"op": "set_param", "key": "club_created_within_days", "value": int(m.group(1))})
        elif m and re.search(r"need[- ]by", sl):
            ops.append({"op": "set_param", "key": "club_need_by_within_days", "value": int(m.group(1))})
        m2 = re.search(r"(\d+)\s*hours", sl)
        if m2 and re.search(r"po|consolid|combine", sl):
            ops.append({"op": "set_param", "key": "po_consolidation_window_hours", "value": int(m2.group(1))})
        m3 = re.search(r"(\d+)\s*days", sl)
        if m3 and re.search(r"preferred (?:supplier )?list", sl):
            ops.append({"op": "set_param", "key": "preferred_list_lookback_days", "value": int(m3.group(1))})
        elif m3 and re.search(r"bought|purchase history|purchased", sl) and re.search(r"last|recent|within", sl) and not re.search(r"club", sl):
            ops.append({"op": "set_param", "key": "recent_purchase_days", "value": int(m3.group(1))})
    for op in ops:                                     # drop Nones
        for k in [k for k, v in op.items() if v is None]:
            op.pop(k)
    # de-duplicate identical set_* operations: last one wins
    seen: dict = {}
    final = []
    for op in ops:
        if op["op"] in ("set_limit", "set_param"):
            seen[(op["op"], op["key"])] = op
        else:
            final.append(op)
    final = list(seen.values()) + final
    return {"summary": f"{len(final)} change(s) read from the document by the built-in reader (no AI key configured).", "effective_date": parse_date_text(text),
            "assumptions": assumptions, "operations": final}


# --------------------------------------------------------------------------- orchestration
def propose(text: str, base: dict, categories: list[str], existing_versions: set[str], effective_override: str | None = None,
            today: dt.date | None = None) -> dict:
    """Return {new_doc, patch, summary, diff, errors, warnings, engine}"""
    today = today or dt.date.today()
    warnings: list[str] = []
    patch, engine = None, "builtin"
    if llm.available():
        try:
            patch = llm_patch(text, base)
            engine = "llm"
        except Exception as e:  # network/key/format problems: fall back, but say so
            warnings.append(f"AI reader unavailable ({type(e).__name__}); used the built-in reader instead.")
    if patch is None:
        patch = fallback_patch(text, base, categories)
    eff = effective_override or patch.get("effective_date") or parse_date_text(text)
    if not eff:
        eff = today.isoformat()
        warnings.append(f"No effective date found in the document; defaulting to today ({eff}). Change it before approving if needed.")
    try:
        dt.date.fromisoformat(eff)
    except Exception:
        warnings.append(f"Effective date '{eff}' is invalid; defaulting to today.")
        eff = today.isoformat()
    version = str(patch.get("version") or next_version(base["version"], existing_versions))
    if version in existing_versions:
        version = next_version(base["version"], existing_versions)
    errors: list[str] = []
    try:
        new_doc = apply_patch(base, patch, version, eff)
        errors = P.validate(new_doc)
    except PatchError as e:
        new_doc = copy.deepcopy(base)
        new_doc["version"], new_doc["effective_date"] = version, eff
        errors = [f"The proposed change could not be applied: {e}"]
    if not patch.get("operations"):
        warnings.append("No policy change was recognised in this document. Check the text, or use the rule editor to describe the change.")
    diff = P.diff_policies(base, new_doc)
    for a in patch.get("assumptions", []) or []:
        warnings.append(f"Assumption: {a}")
    return {"new_doc": new_doc, "patch": patch, "summary": patch.get("summary", ""), "diff": diff, "errors": errors, "warnings": warnings, "engine": engine}
