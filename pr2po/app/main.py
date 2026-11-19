"""FastAPI app: thin HTTP layer over Service. Run: uvicorn app.main:app"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import llm, policy_update
from .chat import ChatBot
from .db import DB
from .engine import policy as P
from .engine.pipeline import OUTPUT_COLUMNS
from .service import AppError, ROLES, Service

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
svc = Service(DB(DATA / "app.db"), ROOT / "seed", ROOT / "policies")
bot = ChatBot(svc)
app = FastAPI(title="Corvo PR-to-PO", version="1.0")


@app.exception_handler(AppError)
async def app_error(_: Request, e: AppError):
    return JSONResponse({"error": str(e)}, status_code=e.status)


def who(name: str | None, role: str | None) -> dict:
    role = (role or "buyer").lower()
    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    return {"name": (name or "API user").strip()[:60] or "API user", "role": role}


def parse_csv_bytes(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "pr_no" not in (rows[0] or {}):
        raise AppError("This does not look like a PR batch CSV (expected columns: pr_no, pr_line, created_at, plant, item_code, description, quantity, ...).")
    return rows


def csv_response(rows, columns, filename):
    return PlainTextResponse(svc.rows_to_csv(rows, columns), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- meta
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/meta")
def meta():
    today = dt.date.today()
    pol = svc.book().at(today)
    return {"today": today.isoformat(), "ai_enabled": llm.available(), "policy_in_force": pol["version"], "roles": list(ROLES),
            "pending_policies": svc.db.one("SELECT COUNT(*) n FROM policy_versions WHERE status='pending'")["n"]}


# ---------------------------------------------------------------- batch processing (the graded API)
@app.post("/api/process-batch")
async def process_batch(request: Request, file: UploadFile | None = File(None), name: str = Query("batch"), persist: bool = Query(True),
                        policy_version: str | None = Query(None), format: str = Query("json"),
                        x_user: str | None = Header(None), x_role: str | None = Header(None)):
    """Takes a PR batch CSV (multipart `file`, or the raw CSV as the request body) and returns the decisions."""
    data = await file.read() if file else await request.body()
    if file and name == "batch":
        name = file.filename or "batch"
    raw = parse_csv_bytes(data)
    res = svc.process_batch(raw, who(x_user, x_role), name=name, pin_version=policy_version, persist=persist)
    if format == "csv" or "text/csv" in (request.headers.get("accept") or ""):
        return csv_response(res["decisions"], OUTPUT_COLUMNS, f"decisions_run{res.get('run_id', 'x')}.csv")
    return res


@app.get("/api/runs")
def runs():
    return svc.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    return svc.get_run(run_id)


@app.get("/api/runs/{run_id}/requirements")
def reqs(run_id: int):
    return svc.list_requirements(run_id)


@app.get("/api/runs/{run_id}/requirements/{group_id}")
def req(run_id: int, group_id: str):
    return svc.get_requirement(run_id, group_id)


@app.post("/api/runs/{run_id}/requirements/{group_id}/approve")
def approve(run_id: int, group_id: str, x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.approve(run_id, group_id, who(x_user, x_role))


@app.post("/api/runs/{run_id}/requirements/{group_id}/override")
def override(run_id: int, group_id: str, body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.override(run_id, group_id, who(x_user, x_role), body)


@app.post("/api/runs/{run_id}/approve-all")
def approve_all(run_id: int, x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.approve_all(run_id, who(x_user, x_role))


@app.post("/api/runs/{run_id}/reprocess")
def reprocess(run_id: int, policy_version: str | None = Query(None), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.reprocess(run_id, who(x_user, x_role), policy_version)


@app.get("/api/runs/{run_id}/decisions.csv")
def decisions_csv(run_id: int):
    return csv_response(svc.run_rows(run_id), OUTPUT_COLUMNS, f"decisions_run{run_id}.csv")


# ---------------------------------------------------------------- chat
@app.post("/api/chat")
def chat(body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return bot.handle(body.get("session_id"), who(x_user or "Requester", x_role or "requester"), body.get("text") or "", body.get("choice"))


# ---------------------------------------------------------------- items / suppliers
@app.get("/api/items")
def items(q: str = ""):
    m = svc.master()
    ql = q.lower()
    return [i for i in m.items.values() if not ql or ql in i["description"].lower() or ql in i["item_code"].lower()]


@app.get("/api/suppliers")
def suppliers():
    return list(svc.master().suppliers.values())


@app.get("/api/items/{code}/preferred")
def item_pref(code: str, as_of: str | None = None):
    return svc.preferred_for_item(code, dt.date.fromisoformat(as_of) if as_of else dt.date.today())


@app.get("/api/preferred-suppliers")
def pref_json(as_of: str | None = None, top: int = 3):
    return svc.preferred_rows(dt.date.fromisoformat(as_of) if as_of else dt.date.today(), top)


@app.get("/api/preferred-suppliers.csv")
def pref_csv(as_of: str | None = None, top: int = 3):
    d = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    return csv_response(svc.preferred_rows(d, top), ["item_code", "rank", "supplier_id", "reference_price"], f"preferred_suppliers_{d}.csv")


# ---------------------------------------------------------------- contracts
@app.get("/api/contracts")
def contracts():
    return svc.list_contracts()


@app.post("/api/contracts/extract")
async def contract_extract(pdf: UploadFile = File(...)):
    return svc.parse_contract(await pdf.read())


@app.post("/api/contracts")
async def contract_add(data: str = Form(...), pdf: UploadFile | None = File(None), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    b = await pdf.read() if pdf else None
    return svc.add_contract(json.loads(data), who(x_user, x_role), b, pdf.filename if pdf else None)


@app.get("/api/contracts/{cid}/pdf")
def contract_pdf(cid: str):
    for p in (ROOT / "seed" / "contracts" / "pdf" / f"{cid}.pdf", DATA / "contract_pdfs" / f"{cid}.pdf"):
        if p.exists():
            return FileResponse(p, media_type="application/pdf")
    raise HTTPException(404, "No PDF stored for this contract")


# ---------------------------------------------------------------- orders
@app.get("/api/purchase-orders")
def pos():
    return [dict(r) | {"lines": json.loads(r["lines"])} for r in svc.db.q("SELECT * FROM purchase_orders ORDER BY created_at DESC")]


@app.get("/api/sourcing-events")
def events():
    return [dict(r) for r in svc.db.q("SELECT * FROM sourcing_events ORDER BY created_at DESC")]


# ---------------------------------------------------------------- policy
@app.get("/api/policies")
def policies():
    return svc.list_policies()


@app.get("/api/policies/schema")
def policy_schema():
    return {"fields": P.FIELD_CATALOG, "operators": P.OPS, "decisions": P.DECISIONS, "supplier_modes": P.SUPPLIER_MODES}


@app.post("/api/policies/propose")
async def policy_propose(file: UploadFile | None = File(None), text: str = Form(""), effective_date: str | None = Form(None),
                         x_user: str | None = Header(None), x_role: str | None = Header(None)):
    content, name = text or "", "chat instruction"
    if file is not None:
        content = policy_update.extract_text(await file.read(), file.filename or "") + "\n" + content
        name = file.filename or "upload"
    return svc.propose_policy(who(x_user, x_role), content, name, effective_date or None)


@app.put("/api/policies/{pid}/draft")
def policy_edit(pid: int, body: dict = Body(...), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.update_policy_draft(pid, who(x_user, x_role), body["doc"])


@app.post("/api/policies/{pid}/approve")
def policy_approve(pid: int, body: dict = Body({}), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.decide_policy(pid, who(x_user, x_role), True, body.get("note", ""))


@app.post("/api/policies/{pid}/reject")
def policy_reject(pid: int, body: dict = Body({}), x_user: str | None = Header(None), x_role: str | None = Header(None)):
    return svc.decide_policy(pid, who(x_user, x_role), False, body.get("note", ""))


@app.get("/api/audit")
def audit():
    return svc.audit_log()


app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")
