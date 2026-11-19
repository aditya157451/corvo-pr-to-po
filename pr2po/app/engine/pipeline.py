"""The decision engine. Pure functions: (PR lines, master data, policy book) -> decisions.
No I/O, no web, no DB. All business thresholds come from the policy document."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from .master import Master, Contract
from .matching import ItemMatcher
from .policy import PolicyBook, select_rule
from .suppliers import preferred_list, suggest_suppliers

EPS = 1e-6


def inr(x: float | None) -> str:
    return "n/a" if x is None else f"₹{x:,.2f}".replace(".00", "")


# --------------------------------------------------------------------------- records
@dataclass
class Line:
    idx: int                         # position in the input
    pr_no: str
    pr_line: str
    created_at: dt.datetime | None
    plant: str
    item_code_in: str
    description: str
    quantity: float | None
    uom: str
    est_price: float | None
    need_by: dt.date | None
    urgency: str
    requester: str = ""
    department: str = ""
    # filled by the pipeline
    error: str | None = None
    resolved: str | None = None      # item code or None for NEW
    match_method: str = ""
    match_note: str = ""
    flags: list[str] = field(default_factory=list)

    @property
    def sort_key(self):
        try:
            ln = int(self.pr_line)
        except ValueError:
            ln = 0
        return (self.created_at or dt.datetime.max, self.pr_no, ln)


@dataclass
class Requirement:
    req_no: int
    item: str | None                 # None => NEW
    plant: str
    lines: list[Line]
    first_created: dt.datetime
    first_need_by: dt.date | None

    @property
    def date(self) -> dt.date:
        return self.first_created.date()

    @property
    def quantity(self) -> float:
        return sum(l.quantity or 0 for l in self.lines)

    @property
    def is_emergency(self) -> bool:
        return any(l.urgency.strip().lower() == "emergency" for l in self.lines)

    @property
    def group_id(self) -> str:
        return f"REQ-{self.req_no:04d}"


@dataclass
class Decision:
    requirement: Requirement
    policy_version: str = ""
    decision: str = ""
    route: str = ""
    rule_id: str = ""
    rule_title: str = ""
    value: float | None = None
    unit_price: float | None = None
    supplier_id: str | None = None
    suggested: list[str] = field(default_factory=list)
    po_id: str | None = None
    contract_id: str | None = None
    relied_on: str = ""
    reason: str = ""
    requires_approval: str | None = None
    explain: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    lines: list[Line]
    requirements: list[Requirement]
    decisions: list[Decision]
    pos: list[dict]                  # [{po_id, supplier_id, plant, route, requirements:[group_id], value}]
    contract_usage: dict[str, dict]
    rows: list[dict]                 # one output row per input line


# --------------------------------------------------------------------------- parsing
def _num(v) -> float | None:
    s = str(v if v is not None else "").strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _dt(v) -> dt.datetime | None:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def parse_lines(raw: list[dict], master: Master) -> list[Line]:
    lines: list[Line] = []
    seen: set[tuple] = set()
    for i, r in enumerate(raw):
        g = lambda k: str(r.get(k, "") or "").strip()
        ln = Line(i, g("pr_no"), g("pr_line"), _dt(g("created_at")), g("plant").upper(), g("item_code").upper(),
                  g("description"), _num(r.get("quantity")), g("uom"), _num(r.get("estimated_unit_price")),
                  (lambda x: x.date() if x else None)(_dt(g("need_by_date"))), g("urgency") or "Normal",
                  g("requester"), g("department"))
        key = (ln.pr_no, ln.pr_line)
        if not ln.pr_no:
            ln.error = "missing PR number"
        elif key in seen:
            ln.error = "duplicate PR number and line in the batch (first occurrence kept)"
        elif ln.created_at is None:
            ln.error = f"created_at '{g('created_at')}' is missing or not a valid date/time"
        elif ln.plant not in master.plants:
            ln.error = f"plant '{ln.plant or '(blank)'}' is not a Corvo plant (valid: {', '.join(master.plants)}); cannot assume which plant was meant"
        elif ln.quantity is None or ln.quantity <= 0:
            ln.error = f"quantity '{g('quantity')}' is not a positive number"
        elif not ln.item_code_in and not ln.description:
            ln.error = "no item code and no description"
        seen.add(key)
        if ln.urgency.lower() not in ("normal", "emergency"):
            ln.flags.append(f"urgency '{ln.urgency}' not recognised; treated as Normal")
        lines.append(ln)
    return lines


_URGENT_WORDS = re.compile(r"\b(urgent|urgently|asap|emergency|immediately|breakdown)\b", re.I)


# --------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, master: Master, book: PolicyBook):
        self.m = master
        self.book = book
        self.matcher = ItemMatcher(master.items)

    # ---- step 1: validate + match
    def prepare(self, raw: list[dict]) -> list[Line]:
        lines = parse_lines(raw, self.m)
        for ln in lines:
            if ln.error:
                continue
            res = self.matcher.match(ln.item_code_in, ln.description)
            ln.resolved, ln.match_method, ln.match_note = res.item_code, res.method, res.note
            if ln.resolved is None:
                ln.flags.append("NEW item: " + (res.note or "no match in the item master"))
            elif res.method == "text":
                ln.flags.append(f"matched on description to {res.item_code} ({res.confidence:.0%})")
            if ln.urgency.lower() == "normal" and _URGENT_WORDS.search(ln.description or ""):
                ln.flags.append("description says urgent but urgency field is Normal: treated as Normal; buyer may override")
        return lines

    # ---- step 2: clubbing (policy 2.2)
    def club(self, lines: list[Line]) -> list[Requirement]:
        reqs: list[Requirement] = []
        for ln in sorted((l for l in lines if not l.error), key=lambda l: l.sort_key):
            joined = None
            if ln.resolved is not None:                              # new items are never clubbed (1.2)
                for rq in reqs:                                      # earliest qualifying requirement
                    if rq.item != ln.resolved or rq.plant != ln.plant:
                        continue
                    pol = self.book.at(rq.date)["params"]
                    if (ln.created_at.date() - rq.date).days > pol["club_created_within_days"]:
                        continue
                    if rq.first_need_by is None or ln.need_by is None:
                        if rq.first_need_by != ln.need_by:
                            continue
                    elif abs((ln.need_by - rq.first_need_by).days) > pol["club_need_by_within_days"]:
                        continue
                    joined = rq
                    break
            if joined:
                joined.lines.append(ln)
            else:
                reqs.append(Requirement(len(reqs) + 1, ln.resolved, ln.plant, [ln], ln.created_at, ln.need_by))
        return reqs

    # ---- step 3..6
    def run(self, raw: list[dict], pin_version: str | None = None) -> RunResult:
        book_backup = self.book
        if pin_version:
            self.book = self.book.pinned(pin_version)
        try:
            return self._run(raw)
        finally:
            self.book = book_backup

    def _run(self, raw: list[dict]) -> RunResult:
        lines = self.prepare(raw)
        reqs = self.club(lines)
        consumed = {c.contract_id: c.consumed for c in self.m.contracts}
        decisions: dict[int, Decision] = {}
        # requirements consume contract ceilings in requirement-date order (4.2) = creation order
        for rq in sorted(reqs, key=lambda r: (r.first_created, r.lines[0].pr_no, r.lines[0].pr_line)):
            decisions[rq.req_no] = self._decide(rq, consumed)
        ordered = [decisions[r.req_no] for r in reqs]
        pos = self._consolidate(ordered)
        usage = {c.contract_id: {"cap": c.cap, "register_consumed": c.consumed, "consumed_after_run": round(consumed[c.contract_id], 2),
                                 "run_consumed": round(consumed[c.contract_id] - c.consumed, 2)} for c in self.m.contracts}
        rows = self._rows(lines, ordered)
        return RunResult(lines, reqs, ordered, pos, usage, rows)

    # ---------------------------------------------------------------- decide one requirement
    def _decide(self, rq: Requirement, consumed: dict[str, float]) -> Decision:
        pol = self.book.at(rq.date)
        params = pol["params"]
        dec = Decision(rq, policy_version=str(pol["version"]))
        dec.flags = [f for l in rq.lines for f in l.flags]
        item = self.m.items.get(rq.item) if rq.item else None
        ranking = pol.get("ranking")
        lookback = params.get("preferred_list_lookback_days", 548)
        ex: dict = {"policy_effective": pol["effective_date"], "lines": [f"{l.pr_no}/{l.pr_line}" for l in rq.lines]}
        dec.explain = ex

        # ---------- valuation + contracts (policy 3.1 / 4)
        contract_info = None
        if item is None:
            est = next((l.est_price for l in rq.lines if l.est_price is not None), None)
            prices = [l.est_price for l in rq.lines if l.est_price is not None]
            est = prices[0] if prices else None
            dec.value = round(rq.quantity * est, 2) if est is not None else None
            dec.unit_price = est
            ex["valuation"] = "requester estimate (1.2)" if est is not None else "no estimate given: value unknown"
            if est is None:
                dec.flags.append("NEW item without an estimated unit price; value unknown, buyer must estimate")
            ctx_contract = dict(contract_usable=False, contract_applicable=False, contract_count=0)
            days_active = days_any = None
            pref: list = []
        else:
            applicable: list[dict] = []
            for c in self.m.contracts_by_item.get(rq.item, []):
                if not (c.start <= rq.date <= c.end):
                    continue
                if not self.m.supplier_active(c.supplier_id):
                    continue
                rate = c.lines[rq.item]
                val_c = rq.quantity * rate
                room = c.cap - consumed[c.contract_id]
                applicable.append({"contract": c, "rate": rate, "value": val_c, "room": room,
                                   "fits": consumed[c.contract_id] + val_c <= c.cap + EPS})
            usable = sorted((a for a in applicable if a["fits"]), key=lambda a: (a["rate"], a["contract"].contract_id))
            contract_info = usable[0] if usable else None
            ex["contracts_considered"] = [{"contract_id": a["contract"].contract_id, "supplier_id": a["contract"].supplier_id,
                                           "rate": a["rate"], "ceiling_room": round(a["room"], 2), "requirement_value_at_rate": round(a["value"], 2),
                                           "fits": a["fits"], "source": a["contract"].source} for a in applicable]
            # excluded contracts (why) help a buyer
            excl = []
            for c in self.m.contracts_by_item.get(rq.item, []):
                if any(a["contract"] is c for a in applicable):
                    continue
                if not (c.start <= rq.date <= c.end):
                    excl.append(f"{c.contract_id} not valid on {rq.date} ({c.start} to {c.end})")
                elif not self.m.supplier_active(c.supplier_id):
                    excl.append(f"{c.contract_id} supplier {c.supplier_id} is {self.m.suppliers[c.supplier_id]['status']}")
            if excl:
                ex["contracts_excluded"] = excl
            if contract_info:
                unit, vsrc = contract_info["rate"], f"3.1(a) rate of contract {contract_info['contract'].contract_id}"
            elif applicable:
                low = min(applicable, key=lambda a: (a["rate"], a["contract"].contract_id))
                unit, vsrc = low["rate"], f"3.1(a) lowest applicable contract rate ({low['contract'].contract_id}); every applicable contract is out of ceiling"
            else:
                hist = self.m.pos_for(rq.item, rq.date, params.get("valuation_po_lookback_days", 365))
                if hist:
                    last = hist[-1]
                    unit, vsrc = last["unit_price"], f"3.1(b) most recent PO {last['po_number']} ({last['po_date']}, {last['supplier_id']})"
                else:
                    unit, vsrc = item["standard_price"], "3.1(c) item master standard price"
            dec.value = round(rq.quantity * unit, 2)
            dec.unit_price = unit
            ex["valuation"] = vsrc
            ex["valuation_unit_price"] = unit
            # purchase history facts for rule conditions
            all_pos = self.m.pos_for(rq.item, rq.date, 100000)
            act = [p for p in all_pos if self.m.supplier_active(p["supplier_id"])]
            days_any = (rq.date - all_pos[-1]["po_date"]).days if all_pos else None
            days_active = (rq.date - act[-1]["po_date"]).days if act else None
            pref = preferred_list(self.m, rq.item, rq.date, lookback, ranking)
            ctx_contract = dict(contract_usable=contract_info is not None, contract_applicable=bool(applicable),
                                contract_count=len(applicable))
            ex["preferred_list"] = [p.as_dict() for p in pref]

        needs = [l.need_by for l in rq.lines if l.need_by]
        ctx = {
            "is_new_item": item is None, "item_code": rq.item, "item_type": item["item_type"] if item else None,
            "category": item["category"] if item else None, "uom": item["uom"] if item else (rq.lines[0].uom or None),
            "plant": rq.plant, "value": dec.value, "quantity": rq.quantity, "line_count": len(rq.lines),
            "is_emergency": rq.is_emergency, "days_since_active_purchase": days_active, "days_since_any_purchase": days_any,
            "preferred_supplier_count": len(pref), "lead_time_days": (min(needs) - rq.date).days if needs else None,
            "department": sorted({l.department for l in rq.lines if l.department}), **ctx_contract,
        }

        def can_apply(then: dict) -> str | None:
            if then.get("decision") == "PO":
                mode = then.get("supplier", "preferred")
                if mode == "contract" and not contract_info:
                    return "no usable contract"
                if mode == "preferred" and len(pref) < int(then.get("supplier_rank", 1)):
                    return "no preferred supplier available"
            return None

        match = select_rule(pol, ctx, can_apply)
        then = match.then
        dec.rule_id, dec.rule_title = match.rule_id, match.title
        dec.decision, dec.route = then["decision"], then["route"]
        dec.requires_approval = then.get("requires_approval")
        if match.skipped:
            dec.flags += match.skipped
        ex["rule_context"] = {k: v for k, v in ctx.items() if k not in ("item_code",)}

        # ---------- outcome details
        cites = f"Rule {match.rule_id} of policy v{pol['version']}"
        if then["decision"] == "PO":
            mode = then.get("supplier", "preferred")
            if mode == "contract":
                c, rate = contract_info["contract"], contract_info["rate"]
                dec.supplier_id, dec.unit_price, dec.contract_id = c.supplier_id, rate, c.contract_id
                consumed[c.contract_id] += contract_info["value"]
                dec.relied_on = f"contract {c.contract_id}"
                dec.reason = (f"{cites} (4.1-4.3): contract {c.contract_id} with {c.supplier_id} covers {rq.item}, valid on {rq.date}, "
                              f"supplier Active, ceiling room {inr(contract_info['room'])} >= requirement {inr(contract_info['value'])}; "
                              f"PO at contract rate {inr(rate)}.")
                if len([a for a in ex["contracts_considered"]]) > 1:
                    dec.reason += " Lowest-rate contract with enough ceiling chosen (4.3)."
            else:
                pick = pref[int(then.get("supplier_rank", 1)) - 1]
                dec.supplier_id = pick.supplier_id
                dec.unit_price = pick.reference_price
                dec.relied_on = f"PO {pick.last_po_number}" if pick.last_po_number else f"contract {pick.contract_id}"
                if pick.contract_id and not pick.last_po_number:
                    dec.contract_id = pick.contract_id
                cond = rule_text = match.title
                dec.reason = (f"{cites}: {cond}. Value {inr(dec.value)}"
                              + (f"; item last bought {days_active} days ago from an Active supplier" if days_active is not None else "")
                              + f". 1st preferred supplier {pick.supplier_id} (score {pick.score:.3f}) at reference price {inr(pick.reference_price)} "
                              f"from {pick.reference_source}.")
        else:
            n = int(then.get("suggest_suppliers", 0))
            if item is not None and n > 0:
                ids, _, topped = suggest_suppliers(self.m, rq.item, rq.date, n, lookback, ranking)
                dec.suggested = ids
                ex["topped_up_from_category"] = topped
            dec.reason = f"{cites}: {match.title}. Value {inr(dec.value)}."
            if item is None:
                dec.reason += " Not in the item master (1.2); never clubbed; valued at requester's estimate; buyer to find suppliers."
            elif dec.suggested:
                dec.reason += f" Suggested {len(dec.suggested)} supplier(s) from the preferred list" + \
                              (f", topped up from category '{item['category']}'" if ex.get("topped_up_from_category") else "") + "."
            if dec.requires_approval:
                dec.reason += f" Needs {dec.requires_approval} approval."
            if item is not None and rq.item and item["item_type"] != "Capex" and ex.get("contracts_considered") and not contract_info:
                dec.reason += " Contract(s) exist but cannot be used: " + "; ".join(
                    f"{a['contract_id']} room {inr(a['ceiling_room'])} < requirement {inr(a['requirement_value_at_rate'])}"
                    for a in ex["contracts_considered"] if not a["fits"]) + "."
        if ex.get("contracts_excluded"):
            dec.reason += " Contracts not applied: " + "; ".join(ex["contracts_excluded"]) + "."
        dec.reason += f" Value basis: {ex['valuation']}."
        if len(rq.lines) > 1:
            dec.reason += f" Clubbed {len(rq.lines)} PR lines (2.2)."
        # ---------- 5.7 hard guard: blocked/inactive suppliers never receive a PO or suggestion
        for sid in ([dec.supplier_id] if dec.supplier_id else []) + dec.suggested:
            assert self.m.supplier_active(sid), f"engine produced inactive supplier {sid}"
        return dec

    # ---------------------------------------------------------------- PO consolidation (policy 7)
    def _consolidate(self, decisions: list[Decision]) -> list[dict]:
        pos: list[dict] = []
        for dec in sorted((d for d in decisions if d.decision == "PO"), key=lambda d: (d.requirement.first_created, d.requirement.lines[0].pr_no)):
            rq = dec.requirement
            window = self.book.at(rq.date)["params"].get("po_consolidation_window_hours", 24)
            target = None
            for po in pos:
                if (po["supplier_id"], po["plant"], po["route"]) == (dec.supplier_id, rq.plant, dec.route) and \
                        (rq.first_created - po["first_created"]) <= dt.timedelta(hours=window):
                    target = po
                    break
            if target is None:
                target = {"po_id": f"PO-{len(pos) + 1:04d}", "supplier_id": dec.supplier_id, "plant": rq.plant, "route": dec.route,
                          "first_created": rq.first_created, "requirements": [], "value": 0.0, "lines": []}
                pos.append(target)
            target["requirements"].append(rq.group_id)
            target["value"] = round(target["value"] + (dec.value or 0), 2)
            target["lines"].append({"group_id": rq.group_id, "item_code": rq.item, "quantity": rq.quantity,
                                    "unit_price": dec.unit_price, "value": dec.value})
            dec.po_id = target["po_id"]
        for po in pos:
            po["first_created"] = po["first_created"].isoformat(sep=" ", timespec="minutes")
        return pos

    # ---------------------------------------------------------------- output rows
    def _rows(self, lines: list[Line], decisions: list[Decision]) -> list[dict]:
        by_line: dict[int, Decision] = {}
        for d in decisions:
            for l in d.requirement.lines:
                by_line[l.idx] = d
        rows = []
        for l in lines:
            base = {"pr_no": l.pr_no, "pr_line": l.pr_line}
            if l.error:
                rows.append({**base, "resolved_item_code": "", "group_id": "", "po_id": "", "decision": "", "route": "NOT_PROCESSED",
                             "supplier_id": "", "unit_price": "", "group_value_inr": "", "suggested_supplier_ids": "",
                             "policy_version": "", "reason": f"NOT_PROCESSED: {l.error}"})
                continue
            d = by_line[l.idx]
            rows.append({**base, "resolved_item_code": l.resolved or "NEW", "group_id": d.requirement.group_id, "po_id": d.po_id or "",
                         "decision": d.decision, "route": d.route, "supplier_id": d.supplier_id or "",
                         "unit_price": "" if d.unit_price is None or d.decision != "PO" else round(d.unit_price, 2),
                         "group_value_inr": "" if d.value is None else round(d.value, 2),
                         "suggested_supplier_ids": ";".join(d.suggested), "policy_version": d.policy_version,
                         "reason": d.reason})
        return rows


OUTPUT_COLUMNS = ["pr_no", "pr_line", "resolved_item_code", "group_id", "po_id", "decision", "route", "supplier_id",
                  "unit_price", "group_value_inr", "suggested_supplier_ids", "policy_version", "reason"]
