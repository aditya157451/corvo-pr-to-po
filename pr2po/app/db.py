"""SQLite persistence. One file, no server to run. Master data, contracts, policy versions,
runs/requirements/POs/sourcing events, audit log and chat sessions all live here."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(item_code TEXT PRIMARY KEY, description TEXT, uom TEXT, category TEXT, item_type TEXT, standard_price REAL);
CREATE TABLE IF NOT EXISTS suppliers(supplier_id TEXT PRIMARY KEY, name TEXT, city TEXT, categories TEXT, status TEXT, on_time_pct REAL, quality_rating REAL);
CREATE TABLE IF NOT EXISTS plants(plant TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS po_history(po_number TEXT PRIMARY KEY, po_date TEXT, supplier_id TEXT, item_code TEXT, quantity REAL, uom TEXT, unit_price REAL, plant TEXT, on_time INTEGER);
CREATE INDEX IF NOT EXISTS ix_po_item ON po_history(item_code, po_date);
CREATE TABLE IF NOT EXISTS contracts(contract_id TEXT PRIMARY KEY, supplier_id TEXT, start_date TEXT, end_date TEXT, value_cap REAL,
  consumed_value REAL DEFAULT 0, source TEXT DEFAULT 'register', pdf_name TEXT, added_by TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS contract_lines(contract_id TEXT, item_code TEXT, rate REAL, PRIMARY KEY(contract_id,item_code));
CREATE TABLE IF NOT EXISTS policy_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT, effective_date TEXT, status TEXT,
  doc TEXT, source_name TEXT, source_text TEXT, summary TEXT, diff TEXT, warnings TEXT, base_id INTEGER,
  created_by TEXT, created_at TEXT, decided_by TEXT, decided_at TEXT, decision_note TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT, parent_run_id INTEGER, policy_mode TEXT,
  created_by TEXT, created_at TEXT, line_count INTEGER, summary TEXT, policy_versions TEXT);
CREATE TABLE IF NOT EXISTS pr_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, pr_no TEXT, pr_line TEXT, created_at TEXT, requester TEXT,
  department TEXT, plant TEXT, item_code TEXT, description TEXT, quantity TEXT, uom TEXT, estimated_unit_price TEXT, need_by_date TEXT, urgency TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS ix_pr_run ON pr_lines(run_id);
CREATE TABLE IF NOT EXISTS requirements(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, group_id TEXT, signature TEXT, status TEXT DEFAULT 'pending',
  decision TEXT, override TEXT, po_id TEXT, event_id TEXT, decided_by TEXT, decided_at TEXT, UNIQUE(run_id, group_id));
CREATE TABLE IF NOT EXISTS run_rows(run_id INTEGER, row_no INTEGER, row TEXT, PRIMARY KEY(run_id,row_no));
CREATE TABLE IF NOT EXISTS purchase_orders(run_id INTEGER, po_id TEXT, supplier_id TEXT, plant TEXT, route TEXT, status TEXT, lines TEXT, value REAL,
  created_by TEXT, created_at TEXT, PRIMARY KEY(run_id,po_id));
CREATE TABLE IF NOT EXISTS sourcing_events(event_id TEXT PRIMARY KEY, run_id INTEGER, group_id TEXT, kind TEXT, item_code TEXT, plant TEXT, value REAL,
  suggested TEXT, status TEXT, detail TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT, role TEXT, action TEXT, entity TEXT, entity_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS chat_sessions(id TEXT PRIMARY KEY, user TEXT, state TEXT, messages TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

_lock = threading.RLock()


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with _lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with _lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql: str, args=()):
        with _lock:
            return self.conn.execute(sql, args)


def jd(v) -> str:
    return json.dumps(v, default=str, ensure_ascii=False)


def jl(s, default=None):
    if s is None or s == "":
        return default
    return json.loads(s)
