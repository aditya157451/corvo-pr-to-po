"""Application layer: wires the pure engine to storage. All workflow rules (who may do what,
what is audited) live here; all procurement rules live in the policy document."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import threading
from pathlib import Path

from .contract_pdf import parse_contract_pdf
from .db import DB, jd, jl
from .engine.master import Contract, Master, d, load_seed
from .engine.pipeline import Engine, OUTPUT_COLUMNS, RunResult
from . import policy_update
from .engine import policy as P
from .engine.policy import PolicyBook, validate, diff_policies
from .engine.suppliers import preferred_list
from .seed import contract_from_parsed

PR_COLUMNS = ["pr_no", "pr_line", "created_at", "requester", "department", "plant", "item_code", "description",
              "quantity", "uom", "estimated_unit_price", "need_by_date", "urgency"]
LIVE_RUN_ID = 1
ROLES = ("requester", "buyer", "procurement")


class AppError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def now() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="seconds")


class Service:
    def __init__(self, db: DB, seed_dir: Path, policies_dir: Path):
        self.db, self.seed_dir, self.policies_dir = db, Path(seed_dir), Path(policies_dir)
        self._master: Master | None = None
        self._lock = threading.RLock()
        self.bootstrap()

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self):
        db = self.db
        if not db.one("SELECT 1 FROM items LIMIT 1"):
            from .seed import load_full_seed
            m = load_full_seed(self.seed_dir)
            with db.tx() as c:
                for i in m.items.values():
                    c.execute("INSERT INTO items VALUES(?,?,?,?,?,?)", (i["item_code"], i["description"], i["uom"], i["category"], i["item_type"], i["standard_price"]))
                for s in m.suppliers.values():
                    c.execute("INSERT INTO suppliers VALUES(?,?,?,?,?,?,?)", (s["supplier_id"], s["name"], s["city"], ";".join(s["categories"]), s["status"], s["on_time_pct"], s["quality_rating"]))
                for p, n in m.plants.items():
                    c.execute("INSERT INTO plants VALUES(?,?)", (p, n))
                for p in m.pos:
                    c.execute("INSERT INTO po_history VALUES(?,?,?,?,?,?,?,?,?)", (p["po_number"], p["po_date"].isoformat(), p["supplier_id"], p["item_code"], p["quantity"], p["uom"], p["unit_price"], p["plant"], int(p["on_time"])))
                for k in m.contracts:
                    c.execute("INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?)", (k.contract_id, k.supplier_id, k.start.isoformat(), k.end.isoformat(), k.cap, k.consumed, k.source, f"{k.contract_id}.pdf" if (self.seed_dir / "contracts" / "pdf" / f"{k.contract_id}.pdf").exists() else None, "system", now()))
                    for it, rate in k.lines.items():
                        c.execute("INSERT INTO contract_lines VALUES(?,?,?)", (k.contract_id, it, rate))
        if not db.one("SELECT 1 FROM policy_versions LIMIT 1"):
            doc = json.loads((self.policies_dir / "policy_v5.0.json").read_text())
            db.x("INSERT INTO policy_versions(version,effective_date,status,doc,source_name,summary,diff,warnings,created_by,created_at,decided_by,decided_at,decision_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (doc["version"], doc["effective_date"], "approved", jd(doc), doc.get("source"), "Initial policy loaded from Corvo_Procurement_Policy.pdf", "[]", "[]", "system", now(), "system", now(), "baseline"))
        if not db.one("SELECT 1 FROM runs WHERE id=?", (LIVE_RUN_ID,)):
            db.x("INSERT INTO runs(id,name,kind,policy_mode,created_by,created_at,line_count,summary,policy_versions) VALUES(?,?,?,?,?,?,?,?,?)",
                 (LIVE_RUN_ID, "Live queue (chat PRs)", "live", "auto", "system", now(), 0, "{}", "[]"))

    # ------------------------------------------------------------------ master data
    def master(self) -> Master:
        with self._lock:
            if self._master is None:
                db = self.db
                items = {r["item_code"]: dict(r) for r in db.q("SELECT * FROM items")}
                sup = {}
                for r in db.q("SELECT * FROM suppliers"):
                    s = dict(r)
                    s["categories"] = [c for c in r["categories"].split(";") if c]
                    sup[r["supplier_id"]] = s
                plants = {r["plant"]: r["name"] for r in db.q("SELECT * FROM plants")}
                pos = [{"po_number": r["po_number"], "po_date": d(r["po_date"]), "supplier_id": r["supplier_id"], "item_code": r["item_code"],
                        "quantity": r["quantity"], "uom": r["uom"], "unit_price": r["unit_price"], "plant": r["plant"], "on_time": bool(r["on_time"])}
                       for r in db.q("SELECT * FROM po_history")]
                lines: dict[str, dict] = {}
                for r in db.q("SELECT * FROM contract_lines"):
                    lines.setdefault(r["contract_id"], {})[r["item_code"]] = r["rate"]
                contracts = [Contract(r["contract_id"], r["supplier_id"], d(r["start_date"]), d(r["end_date"]), r["value_cap"], r["consumed_value"],
                                      lines.get(r["contract_id"], {}), r["source"]) for r in db.q("SELECT * FROM contracts")]
                self._master = Master(items, sup, plants, pos, contracts)
            return self._master

    def invalidate(self):
        with self._lock:
            self._master = None

    # ------------------------------------------------------------------ audit / auth
    def audit(self, user: dict, action: str, entity: str, entity_id: str, detail: dict | None = None):
        self.db.x("INSERT INTO audit_log(ts,user,role,action,entity,entity_id,detail) VALUES(?,?,?,?,?,?,?)",
                  (now(), user["name"], user["role"], action, entity, str(entity_id), jd(detail or {})))

    @staticmethod
    def require(user: dict, *roles: str):
        if user["role"] not in roles:
            raise AppError(f"This action needs the role: {' or '.join(roles)} (you are '{user['role']}').", 403)

    # ------------------------------------------------------------------ policy versions
    def approved_versions(self) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM policy_versions WHERE status='approved' ORDER BY effective_date, id"):
            doc = json.loads(r["doc"])
            doc["_id"] = r["id"]
            out.append(doc)
        return out

    def book(self, extra: list[dict] | None = None) -> PolicyBook:
        return PolicyBook(self.approved_versions() + (extra or []))

    def policy_row(self, r) -> dict:
        today = dt.date.today()
        doc = json.loads(r["doc"])
        in_force = False
        if r["status"] == "approved":
            in_force = self.book().at(today).get("_id") == r["id"]
        return {"id": r["id"], "version": r["version"], "effective_date": r["effective_date"], "status": r["status"], "in_force_today": in_force,
                "source_name": r["source_name"], "summary": r["summary"], "diff": jl(r["diff"], []), "warnings": jl(r["warnings"], []),
                "created_by": r["created_by"], "created_at": r["created_at"], "decided_by": r["decided_by"], "decided_at": r["decided_at"],
                "decision_note": r["decision_note"], "base_id": r["base_id"], "doc": doc,
                "future": r["status"] == "approved" and dt.date.fromisoformat(r["effective_date"]) > today}

    def list_policies(self) -> list[dict]:
        return [self.policy_row(r) for r in self.db.q("SELECT * FROM policy_versions ORDER BY effective_date DESC, id DESC")]

    # ------------------------------------------------------------------ processing
    def _engine(self, extra_versions: list[dict] | None = None) -> Engine:
        return Engine(self.master(), self.book(extra_versions))

    def process_raw(self, raw: list[dict], pin_version: str | None = None) -> RunResult:
        return self._engine().run(raw, pin_version)

    def _requirement_dict(self, res: RunResult, dec) -> dict:
        rq = dec.requirement
        return {
            "group_id": rq.group_id, "item_code": rq.item or "NEW", "plant": rq.plant, "requirement_date": rq.date.isoformat(),
            "first_created": rq.first_created.isoformat(sep=" ", timespec="minutes"), "quantity": rq.quantity, "is_emergency": rq.is_emergency,
            "item_description": (self.master().items[rq.item]["description"] if rq.item else rq.lines[0].description),
            "lines": [{"pr_no": l.pr_no, "pr_line": l.pr_line, "created_at": l.created_at.isoformat(sep=" ", timespec="minutes"), "description": l.description,
                       "quantity": l.quantity, "uom": l.uom, "need_by": l.need_by.isoformat() if l.need_by else None, "urgency": l.urgency,
                       "requester": l.requester, "department": l.department, "match": l.match_method, "match_note": l.match_note} for l in rq.lines],
            "decision": dec.decision, "route": dec.route, "rule_id": dec.rule_id, "rule_title": dec.rule_title, "policy_version": dec.policy_version,
            "value": dec.value, "unit_price": dec.unit_price, "supplier_id": dec.supplier_id, "suggested": dec.suggested, "po_id": dec.po_id,
            "contract_id": dec.contract_id, "relied_on": dec.relied_on, "reason": dec.reason, "requires_approval": dec.requires_approval,
            "flags": dec.flags, "explain": dec.explain,
        }

    @staticmethod
    def _signature(req: dict) -> str:
        key = [(l["pr_no"], l["pr_line"]) for l in req["lines"]]
        return hashlib.sha1(jd([sorted(key), req["route"], req["supplier_id"], round(req["value"] or 0, 2), req["policy_version"]]).encode()).hexdigest()[:16]

    def _store_run(self, run_id: int, res: RunResult, keep_status: bool = False):
        db = self.db
        old = {}
        if keep_status:
            for r in db.q("SELECT * FROM requirements WHERE run_id=?", (run_id,)):
                old[r["group_id"]] = r
            old_by_sig = {r["signature"]: r for r in old.values()}
        db.x("DELETE FROM requirements WHERE run_id=?", (run_id,))
        db.x("DELETE FROM run_rows WHERE run_id=?", (run_id,))
        reopened = []
        for dec in res.decisions:
            req = self._requirement_dict(res, dec)
            sig = self._signature(req)
            status, override, po_id, event_id, by, at = "pending", None, None, None, None, None
            if keep_status:
                prev = old_by_sig.get(sig)
                if prev and prev["status"] != "pending":
                    status, override, po_id, event_id, by, at = prev["status"], prev["override"], prev["po_id"], prev["event_id"], prev["decided_by"], prev["decided_at"]
                else:
                    # same lines under a different group id with an approval => membership/decision changed => re-open
                    for p in old.values():
                        if p["status"] != "pending" and p["signature"] != sig and {tuple(x) for x in jl(p["decision"])["lines_keys"]} & {(l["pr_no"], l["pr_line"]) for l in req["lines"]}:
                            reopened.append(req["group_id"])
                            break
            req["lines_keys"] = [(l["pr_no"], l["pr_line"]) for l in req["lines"]]
            db.x("INSERT INTO requirements(run_id,group_id,signature,status,decision,override,po_id,event_id,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (run_id, req["group_id"], sig, status, jd(req), override, po_id, event_id, by, at))
        for i, row in enumerate(res.rows):
            db.x("INSERT INTO run_rows VALUES(?,?,?)", (run_id, i, jd(row)))
        # NOT_PROCESSED lines are listed on the run too
        return reopened

    def _summary(self, res: RunResult) -> dict:
        routes: dict[str, int] = {}
        for r in res.rows:
            routes[r["route"]] = routes.get(r["route"], 0) + 1
        return {"lines": len(res.rows), "requirements": len(res.requirements), "pos": len(res.pos), "routes": routes,
                "not_processed": routes.get("NOT_PROCESSED", 0), "contract_usage": res.contract_usage,
                "total_value": round(sum(x.value or 0 for x in res.decisions), 2)}

    def process_batch(self, raw: list[dict], user: dict, name: str = "batch", pin_version: str | None = None,
                      parent_run_id: int | None = None, persist: bool = True) -> dict:
        if not raw:
            raise AppError("The batch has no PR lines.")
        res = self.process_raw(raw, pin_version)
        versions = sorted({d_.policy_version for d_ in res.decisions})
        summary = self._summary(res)
        out = {"summary": summary, "policy_versions_used": versions, "decisions": res.rows}
        if not persist:
            return out
        with self.db.tx():
            cur = self.db.x("INSERT INTO runs(name,kind,parent_run_id,policy_mode,created_by,created_at,line_count,summary,policy_versions) VALUES(?,?,?,?,?,?,?,?,?)",
                            (name, "batch", parent_run_id, f"pinned v{pin_version}" if pin_version else "auto", user["name"], now(), len(raw), jd(summary), jd(versions)))
            run_id = cur.lastrowid
            for r in raw:
                self.db.x("INSERT INTO pr_lines(run_id,pr_no,pr_line,created_at,requester,department,plant,item_code,description,quantity,uom,estimated_unit_price,need_by_date,urgency,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (run_id, *[str(r.get(c, "") or "") for c in PR_COLUMNS], "batch"))
            self._store_run(run_id, res)
        self.audit(user, "process_batch", "run", run_id, {"name": name, "lines": len(raw), "policy_versions": versions, "parent": parent_run_id})
        out["run_id"] = run_id
        return out

    def reprocess(self, run_id: int, user: dict, pin_version: str | None = None) -> dict:
        run = self.get_run(run_id)
        raw = self.run_pr_lines(run_id)
        res = self.process_batch(raw, user, name=f"{run['name']} (reprocessed)", pin_version=pin_version, parent_run_id=run_id)
        return res

    # ------------------------------------------------------------------ runs & requirements
    def get_run(self, run_id: int) -> dict:
        r = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not r:
            raise AppError("Run not found", 404)
        out = dict(r)
        out["summary"] = jl(r["summary"], {})
        out["policy_versions"] = jl(r["policy_versions"], [])
        cnt = {s["status"]: s["n"] for s in self.db.q("SELECT status, COUNT(*) n FROM requirements WHERE run_id=? GROUP BY status", (run_id,))}
        out["status_counts"] = cnt
        return out

    def list_runs(self) -> list[dict]:
        return [self.get_run(r["id"]) for r in self.db.q("SELECT id FROM runs ORDER BY id DESC")]

    def run_pr_lines(self, run_id: int) -> list[dict]:
        return [{c: r[c] for c in PR_COLUMNS} for r in self.db.q("SELECT * FROM pr_lines WHERE run_id=? ORDER BY id", (run_id,))]

    def run_rows(self, run_id: int) -> list[dict]:
        self.get_run(run_id)
        return [json.loads(r["row"]) for r in self.db.q("SELECT row FROM run_rows WHERE run_id=? ORDER BY row_no", (run_id,))]

    def list_requirements(self, run_id: int) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM requirements WHERE run_id=? ORDER BY id", (run_id,)):
            dd = json.loads(r["decision"])
            ov = jl(r["override"])
            out.append({"group_id": r["group_id"], "status": r["status"], "item_code": dd["item_code"], "item_description": dd["item_description"],
                        "plant": dd["plant"], "quantity": dd["quantity"], "line_count": len(dd["lines"]), "decision": dd["decision"], "route": dd["route"],
                        "value": dd["value"], "supplier_id": dd["supplier_id"], "unit_price": dd["unit_price"], "suggested": dd["suggested"],
                        "po_id": dd["po_id"], "rule_id": dd["rule_id"], "policy_version": dd["policy_version"], "is_emergency": dd["is_emergency"],
                        "flags": dd["flags"], "requirement_date": dd["requirement_date"], "applied_po": r["po_id"], "applied_event": r["event_id"],
                        "override": ov, "needs_attention": bool(dd["flags"]) or dd["route"] in ("NEW_ITEM_SOURCING",)})
        return out

    def get_requirement(self, run_id: int, group_id: str) -> dict:
        r = self.db.one("SELECT * FROM requirements WHERE run_id=? AND group_id=?", (run_id, group_id))
        if not r:
            raise AppError("Requirement not found", 404)
        dd = jl(r["decision"])
        dd.pop("lines_keys", None)
        hist = [dict(a) | {"detail": jl(a["detail"], {})} for a in self.db.q("SELECT * FROM audit_log WHERE entity='requirement' AND entity_id=? ORDER BY id", (f"{run_id}/{group_id}",))]
        return {"run_id": run_id, "status": r["status"], "override": jl(r["override"]), "applied_po": r["po_id"], "applied_event": r["event_id"],
                "decided_by": r["decided_by"], "decided_at": r["decided_at"], "history": hist, **dd}

    # ------------------------------------------------------------------ approve / override
    def _apply(self, run_id: int, group_id: str, dd: dict, outcome: dict, user: dict) -> tuple[str | None, str | None]:
        """Create the PO (or add this requirement to the consolidated PO) / the sourcing event."""
        db = self.db
        po_id = event_id = None
        if outcome["decision"] == "PO":
            po_id = outcome.get("po_id") or f"PO-X{group_id[-4:]}"
            row = db.one("SELECT * FROM purchase_orders WHERE run_id=? AND po_id=?", (run_id, po_id))
            line = {"group_id": group_id, "item_code": dd["item_code"], "quantity": dd["quantity"], "unit_price": outcome["unit_price"],
                    "value": round((outcome["unit_price"] or 0) * dd["quantity"], 2), "contract_id": dd.get("contract_id"), "relied_on": dd["relied_on"]}
            if row:
                lines = [l for l in jl(row["lines"], []) if l["group_id"] != group_id] + [line]
                db.x("UPDATE purchase_orders SET lines=?, value=? WHERE run_id=? AND po_id=?", (jd(lines), sum(l["value"] for l in lines), run_id, po_id))
            else:
                db.x("INSERT INTO purchase_orders VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, po_id, outcome["supplier_id"], dd["plant"], outcome["route"], "Placed", jd([line]), line["value"], user["name"], now()))
        elif outcome["decision"] == "SOURCING_REQUEST":
            n = db.one("SELECT COUNT(*) n FROM sourcing_events WHERE kind=?", (outcome["route"],))["n"] + 1
            event_id = f"{outcome['route']}-{n:04d}"
            status = "Awaiting capex approval" if dd.get("requires_approval") == "capex" else "Open"
            db.x("INSERT INTO sourcing_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (event_id, run_id, group_id, outcome["route"], dd["item_code"], dd["plant"], dd["value"],
                                                                            ";".join(outcome.get("suggested", [])), status, jd({"reason": dd["reason"], "policy_version": dd["policy_version"]}), user["name"], now()))
        return po_id, event_id

    def approve(self, run_id: int, group_id: str, user: dict) -> dict:
        self.require(user, "buyer", "procurement")
        with self.db.tx():
            r = self.db.one("SELECT * FROM requirements WHERE run_id=? AND group_id=?", (run_id, group_id))
            if not r:
                raise AppError("Requirement not found", 404)
            if r["status"] != "pending":
                raise AppError(f"Already {r['status']}.", 409)
            dd = jl(r["decision"])
            outcome = {k: dd[k] for k in ("decision", "route", "supplier_id", "unit_price", "po_id", "suggested")}
            po_id, event_id = self._apply(run_id, group_id, dd, outcome, user)
            self.db.x("UPDATE requirements SET status='approved', po_id=?, event_id=?, decided_by=?, decided_at=? WHERE run_id=? AND group_id=?", (po_id, event_id, user["name"], now(), run_id, group_id))
        self.audit(user, "approve", "requirement", f"{run_id}/{group_id}", {"route": dd["route"], "po": po_id, "event": event_id, "policy_version": dd["policy_version"]})
        return self.get_requirement(run_id, group_id)

    def approve_all(self, run_id: int, user: dict, only_clean: bool = True) -> dict:
        self.require(user, "buyer", "procurement")
        done, skipped = [], []
        for r in self.list_requirements(run_id):
            if r["status"] != "pending":
                continue
            if only_clean and r["flags"]:
                skipped.append(r["group_id"])
                continue
            self.approve(run_id, r["group_id"], user)
            done.append(r["group_id"])
        self.audit(user, "approve_all", "run", run_id, {"approved": len(done), "left_for_review": len(skipped)})
        return {"approved": done, "left_for_review": skipped}

    def override(self, run_id: int, group_id: str, user: dict, body: dict) -> dict:
        self.require(user, "buyer", "procurement")
        comment = (body.get("comment") or "").strip()
        if len(comment) < 5:
            raise AppError("An override needs a written reason (at least 5 characters).")
        decision = body.get("decision")
        if decision not in ("PO", "SOURCING_REQUEST", "REJECT"):
            raise AppError("decision must be PO, SOURCING_REQUEST or REJECT")
        m = self.master()
        with self.db.tx():
            r = self.db.one("SELECT * FROM requirements WHERE run_id=? AND group_id=?", (run_id, group_id))
            if not r:
                raise AppError("Requirement not found", 404)
            if r["status"] not in ("pending",):
                raise AppError(f"Already {r['status']}.", 409)
            dd = jl(r["decision"])
            outcome: dict = {"decision": decision, "route": body.get("route") or dd["route"], "supplier_id": None, "unit_price": None, "suggested": []}
            if decision == "PO":
                sid = body.get("supplier_id")
                if sid not in m.suppliers:
                    raise AppError("Choose a supplier for the PO.")
                if not m.supplier_active(sid):
                    raise AppError(f"Supplier {sid} is {m.suppliers[sid]['status']}: Blocked/Inactive suppliers cannot receive a PO (policy 5.7).", 422)
                price = body.get("unit_price")
                if price in (None, ""):
                    raise AppError("Enter the unit price for the PO.")
                outcome.update(supplier_id=sid, unit_price=float(price), route=body.get("route") or "SPOT_PO", po_id=None)
                outcome["po_id"] = f"PO-O{group_id[-4:]}"          # an overridden PO is never merged with engine-consolidated POs
            elif decision == "SOURCING_REQUEST":
                sug = [s for s in (body.get("suggested") or []) if s]
                for s in sug:
                    if s not in m.suppliers or not m.supplier_active(s):
                        raise AppError(f"Supplier {s} is not Active and cannot be suggested (policy 5.7).", 422)
                outcome.update(route=body.get("route") or "RFQ", suggested=sug)
            po_id, event_id = (None, None) if decision == "REJECT" else self._apply(run_id, group_id, dd, outcome, user)
            ov = {"comment": comment, "by": user["name"], "at": now(), "original": {k: dd[k] for k in ("decision", "route", "supplier_id", "unit_price", "suggested", "po_id", "rule_id", "policy_version")}, "new": outcome}
            self.db.x("UPDATE requirements SET status=?, override=?, po_id=?, event_id=?, decided_by=?, decided_at=? WHERE run_id=? AND group_id=?",
                      ("rejected" if decision == "REJECT" else "overridden", jd(ov), po_id, event_id, user["name"], now(), run_id, group_id))
        self.audit(user, "override", "requirement", f"{run_id}/{group_id}", {"comment": comment, "original_route": dd["route"], "new": outcome})
        return self.get_requirement(run_id, group_id)

    # ------------------------------------------------------------------ live queue (chat)
    def add_live_lines(self, lines: list[dict], user: dict) -> dict:
        with self.db.tx():
            for r in lines:
                self.db.x("INSERT INTO pr_lines(run_id,pr_no,pr_line,created_at,requester,department,plant,item_code,description,quantity,uom,estimated_unit_price,need_by_date,urgency,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (LIVE_RUN_ID, *[str(r.get(c, "") or "") for c in PR_COLUMNS], "chat"))
        return self.refresh_live(user)

    def next_pr_no(self) -> str:
        n = self.db.one("SELECT COUNT(DISTINCT pr_no) n FROM pr_lines WHERE run_id=? AND source='chat'", (LIVE_RUN_ID,))["n"] + 1
        return f"PR-26-C{n:04d}"

    def refresh_live(self, user: dict) -> dict:
        raw = self.run_pr_lines(LIVE_RUN_ID)
        if not raw:
            return {}
        res = self.process_raw(raw)
        with self.db.tx():
            reopened = self._store_run(LIVE_RUN_ID, res, keep_status=True)
            summary = self._summary(res)
            self.db.x("UPDATE runs SET line_count=?, summary=?, policy_versions=? WHERE id=?", (len(raw), jd(summary), jd(sorted({x.policy_version for x in res.decisions})), LIVE_RUN_ID))
        for g in reopened:
            self.audit(user, "reopen", "requirement", f"{LIVE_RUN_ID}/{g}", {"reason": "a new PR line joined an already-approved requirement"})
        return {"summary": summary, "reopened": reopened}

    # ------------------------------------------------------------------ preferred suppliers
    def preferred_rows(self, as_of: dt.date, top: int = 3) -> list[dict]:
        m = self.master()
        pol = self.book().at(as_of)
        rows = []
        for code in sorted(m.items):
            pl = preferred_list(m, code, as_of, pol["params"].get("preferred_list_lookback_days", 548), pol.get("ranking"))
            for p in pl[:top]:
                rows.append({"item_code": code, "rank": p.rank, "supplier_id": p.supplier_id, "reference_price": p.reference_price})
        return rows

    def preferred_for_item(self, code: str, as_of: dt.date) -> dict:
        m = self.master()
        if code not in m.items:
            raise AppError("Unknown item", 404)
        pol = self.book().at(as_of)
        pl = preferred_list(m, code, as_of, pol["params"].get("preferred_list_lookback_days", 548), pol.get("ranking"))
        excluded = []
        for p in m.pos_for(code, as_of, pol["params"].get("preferred_list_lookback_days", 548)):
            if not m.supplier_active(p["supplier_id"]) and p["supplier_id"] not in [e["supplier_id"] for e in excluded]:
                excluded.append({"supplier_id": p["supplier_id"], "status": m.suppliers[p["supplier_id"]]["status"], "reason": "Blocked/Inactive suppliers are excluded (5.7)"})
        return {"item": m.items[code], "as_of": as_of.isoformat(), "policy_version": pol["version"], "weights": pol.get("ranking", {}).get("weights"),
                "list": [dict(p.as_dict(), name=m.suppliers[p.supplier_id]["name"], on_time_pct=m.suppliers[p.supplier_id]["on_time_pct"],
                              quality_rating=m.suppliers[p.supplier_id]["quality_rating"]) for p in pl], "excluded": excluded}

    # ------------------------------------------------------------------ contracts
    def list_contracts(self) -> list[dict]:
        out = []
        runs_use = self.master()
        for r in self.db.q("SELECT * FROM contracts ORDER BY start_date, contract_id"):
            lines = [dict(l) for l in self.db.q("SELECT cl.item_code, cl.rate, i.description, i.uom FROM contract_lines cl LEFT JOIN items i USING(item_code) WHERE contract_id=? ORDER BY item_code", (r["contract_id"],))]
            sup = runs_use.suppliers.get(r["supplier_id"], {})
            out.append(dict(r) | {"supplier_name": sup.get("name"), "supplier_status": sup.get("status"), "lines": lines, "has_pdf": bool(r["pdf_name"])})
        return out

    def parse_contract(self, pdf_bytes: bytes) -> dict:
        try:
            return parse_contract_pdf(io.BytesIO(pdf_bytes))
        except Exception as e:  # unreadable PDF
            raise AppError(f"Could not read the PDF: {e}")

    def add_contract(self, body: dict, user: dict, pdf_bytes: bytes | None = None, pdf_name: str | None = None) -> dict:
        self.require(user, "buyer", "procurement")
        m = self.master()
        cid = (body.get("contract_id") or "").strip()
        if not cid:
            raise AppError("Contract number is required.")
        if self.db.one("SELECT 1 FROM contracts WHERE contract_id=?", (cid,)):
            raise AppError(f"Contract {cid} already exists.", 409)
        sid = body.get("supplier_id")
        if sid not in m.suppliers:
            raise AppError(f"Unknown supplier '{sid}'.")
        try:
            start, end = d(body["start_date"]), d(body["end_date"])
            cap = float(body["value_cap"])
        except Exception:
            raise AppError("Start date, end date and value ceiling are required and must be valid.")
        if end < start:
            raise AppError("End date is before start date.")
        lines = body.get("lines") or []
        if not lines:
            raise AppError("Add at least one item rate.")
        for l in lines:
            if l["item_code"] not in m.items:
                raise AppError(f"Unknown item code {l['item_code']}.")
        with self.db.tx():
            self.db.x("INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?)", (cid, sid, start.isoformat(), end.isoformat(), cap, float(body.get("consumed_value") or 0), "pdf" if pdf_name else "manual", pdf_name, user["name"], now()))
            for l in lines:
                self.db.x("INSERT INTO contract_lines VALUES(?,?,?)", (cid, l["item_code"], float(l["rate"])))
        if pdf_bytes:
            (self.seed_dir.parent / "data" / "contract_pdfs").mkdir(parents=True, exist_ok=True)
            (self.seed_dir.parent / "data" / "contract_pdfs" / f"{cid}.pdf").write_bytes(pdf_bytes)
        self.invalidate()
        self.audit(user, "add_contract", "contract", cid, {"supplier": sid, "start": str(start), "end": str(end), "cap": cap, "lines": len(lines), "source": "pdf" if pdf_name else "manual"})
        return {"contract_id": cid}

    # ------------------------------------------------------------------ exports
    @staticmethod
    def rows_to_csv(rows: list[dict], columns: list[str]) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    def audit_log(self, limit: int = 300) -> list[dict]:
        return [dict(a) | {"detail": jl(a["detail"], {})} for a in self.db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ policy workflow
    def latest_approved(self) -> dict:
        vs = self.approved_versions()
        today = dt.date.today()
        return self.book().at(today) if vs else None

    def propose_policy(self, user: dict, text: str, source_name: str, effective_override: str | None = None) -> dict:
        self.require(user, "procurement")
        if not text.strip():
            raise AppError("Upload a document or describe the change.")
        base = max(self.approved_versions(), key=lambda v: (v["effective_date"], v["_id"]))
        base_row = self.db.one("SELECT id FROM policy_versions WHERE id=?", (base["_id"],))
        cats = sorted({i["category"] for i in self.master().items.values()})
        existing = {r["version"] for r in self.db.q("SELECT version FROM policy_versions")}
        res = policy_update.propose(text, base, cats, existing, effective_override)
        impact = self._impact(res["new_doc"]) if not res["errors"] else None
        warnings = res["warnings"] + ([f"Validation: {e}" for e in res["errors"]])
        cur = self.db.x("INSERT INTO policy_versions(version,effective_date,status,doc,source_name,source_text,summary,diff,warnings,base_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (res["new_doc"]["version"], res["new_doc"]["effective_date"], "pending", jd(res["new_doc"]), source_name, text[:60000],
                         res["summary"], jd(res["diff"]), jd(warnings), base_row["id"], user["name"], now()))
        pid = cur.lastrowid
        self.audit(user, "propose_policy", "policy", pid, {"version": res["new_doc"]["version"], "effective": res["new_doc"]["effective_date"], "reader": res["engine"], "changes": len(res["diff"])})
        out = self.get_policy(pid)
        out["impact"] = impact
        out["reader"] = res["engine"]
        return out

    def get_policy(self, pid: int) -> dict:
        r = self.db.one("SELECT * FROM policy_versions WHERE id=?", (pid,))
        if not r:
            raise AppError("Policy version not found", 404)
        return self.policy_row(r)

    def update_policy_draft(self, pid: int, user: dict, doc: dict) -> dict:
        self.require(user, "procurement")
        r = self.db.one("SELECT * FROM policy_versions WHERE id=?", (pid,))
        if not r or r["status"] != "pending":
            raise AppError("Only a pending draft can be edited.", 409)
        doc = dict(doc)
        doc.pop("_id", None)
        errs = validate(doc)
        if errs:
            raise AppError("Policy is not valid: " + "; ".join(errs), 422)
        base = json.loads(self.db.one("SELECT doc FROM policy_versions WHERE id=?", (r["base_id"],))["doc"])
        diff = diff_policies(base, doc)
        self.db.x("UPDATE policy_versions SET doc=?, effective_date=?, version=?, diff=?, summary=? WHERE id=?",
                  (jd(doc), doc["effective_date"], doc["version"], jd(diff), (r["summary"] or "") + " (edited manually)", pid))
        self.audit(user, "edit_policy_draft", "policy", pid, {"changes": len(diff)})
        out = self.get_policy(pid)
        out["impact"] = self._impact(doc)
        return out

    def decide_policy(self, pid: int, user: dict, approve: bool, note: str = "") -> dict:
        self.require(user, "procurement")
        r = self.db.one("SELECT * FROM policy_versions WHERE id=?", (pid,))
        if not r:
            raise AppError("Policy version not found", 404)
        if r["status"] != "pending":
            raise AppError(f"This version is already {r['status']}.", 409)
        if approve:
            errs = validate(json.loads(r["doc"]))
            if errs:
                raise AppError("Cannot approve an invalid policy: " + "; ".join(errs), 422)
        self.db.x("UPDATE policy_versions SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                  ("approved" if approve else "rejected", user["name"], now(), note, pid))
        self.audit(user, "approve_policy" if approve else "reject_policy", "policy", pid, {"version": r["version"], "effective": r["effective_date"], "note": note})
        return self.get_policy(pid)

    def _impact(self, new_doc: dict, limit_examples: int = 8) -> dict:
        """What would change if this draft were active: re-run the latest batch (or the sample) under old vs new."""
        runs = self.db.q("SELECT id FROM runs WHERE kind='batch' ORDER BY id DESC LIMIT 1")
        if runs:
            raw, basis = self.run_pr_lines(runs[0]["id"]), f"latest batch (run #{runs[0]['id']})"
        else:
            import csv as _csv
            with open(self.seed_dir / "pr_batches" / "sample_pr_batch.csv", newline="", encoding="utf-8-sig") as f:
                raw, basis = list(_csv.DictReader(f)), "the sample PR batch"
        old = Engine(self.master(), self.book()).run(raw)
        draft = {k: v for k, v in new_doc.items() if k != "_id"}
        new = Engine(self.master(), self.book([draft])).run(raw)
        o = {(r["pr_no"], r["pr_line"]): r for r in old.rows}
        changed, trans, ex = 0, {}, []
        for r in new.rows:
            a = o[(r["pr_no"], r["pr_line"])]
            if (a["route"], a["supplier_id"], a["group_id"] == r["group_id"]) != (r["route"], r["supplier_id"], True) or a["group_value_inr"] != r["group_value_inr"]:
                changed += 1
                key = f"{a['route']} → {r['route']}" if a["route"] != r["route"] else "same route, different supplier/value/clubbing"
                trans[key] = trans.get(key, 0) + 1
                if len(ex) < limit_examples:
                    ex.append({"pr_no": r["pr_no"], "pr_line": r["pr_line"], "before": f"{a['route']} {a['supplier_id']}".strip(), "after": f"{r['route']} {r['supplier_id']}".strip()})
        applies = [x for x in new.decisions if x.policy_version == str(draft["version"])]
        return {"basis": basis, "lines": len(raw), "lines_changed": changed, "transitions": trans, "examples": ex,
                "requirements_under_new_version": len(applies),
                "note": None if applies else f"No requirement in {basis} falls on or after the effective date {draft['effective_date']}, so nothing would change for it."}
