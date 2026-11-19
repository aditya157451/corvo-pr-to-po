"""Master data container + CSV loaders. The engine only ever sees a `Master`."""
from __future__ import annotations

import csv
import datetime as dt
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


@dataclass
class Contract:
    contract_id: str
    supplier_id: str
    start: dt.date
    end: dt.date
    cap: float
    consumed: float = 0.0               # as at last register update (PDF-only contracts start at 0)
    lines: dict[str, float] = field(default_factory=dict)   # item_code -> rate
    source: str = "register"            # register | pdf | manual


@dataclass
class Master:
    items: dict[str, dict]
    suppliers: dict[str, dict]
    plants: dict[str, str]
    pos: list[dict]
    contracts: list[Contract]

    def __post_init__(self):
        self.reindex()

    def reindex(self):
        self.pos_by_item: dict[str, list[dict]] = {}
        for p in sorted(self.pos, key=lambda p: (p["po_date"], p["po_number"])):
            self.pos_by_item.setdefault(p["item_code"], []).append(p)
        self._po_dates = {k: [p["po_date"] for p in v] for k, v in self.pos_by_item.items()}
        self.contracts_by_item: dict[str, list[Contract]] = {}
        for c in self.contracts:
            for it in c.lines:
                self.contracts_by_item.setdefault(it, []).append(c)

    def pos_for(self, item: str, as_of: dt.date, lookback_days: int) -> list[dict]:
        """POs for item with as_of-lookback <= po_date <= as_of, oldest first."""
        dates = self._po_dates.get(item, [])
        hi = bisect_right(dates, as_of)
        lo_date = as_of - dt.timedelta(days=lookback_days)
        return [p for p in self.pos_by_item.get(item, [])[:hi] if p["po_date"] >= lo_date]

    def supplier_active(self, sid: str) -> bool:
        s = self.suppliers.get(sid)
        return bool(s) and s["status"].strip().lower() == "active"


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def load_seed(seed_dir: Path) -> Master:
    md, cd = seed_dir / "master_data", seed_dir / "contracts"
    items = {r["item_code"]: {"item_code": r["item_code"], "description": r["description"], "uom": r["uom"],
                              "category": r["category"], "item_type": r["item_type"],
                              "standard_price": float(r["standard_price"])} for r in _rows(md / "item_master.csv")}
    suppliers = {r["supplier_id"]: {"supplier_id": r["supplier_id"], "name": r["name"], "city": r["city"],
                                    "categories": [c.strip() for c in r["categories"].split(";") if c.strip()],
                                    "status": r["status"], "on_time_pct": float(r["on_time_pct"]),
                                    "quality_rating": float(r["quality_rating"])} for r in _rows(md / "supplier_master.csv")}
    plants = {r["plant"]: r["name"] for r in _rows(md / "plants.csv")}
    pos = [{"po_number": r["po_number"], "po_date": d(r["po_date"]), "supplier_id": r["supplier_id"],
            "item_code": r["item_code"], "quantity": float(r["quantity"]), "uom": r["uom"],
            "unit_price": float(r["unit_price"]), "plant": r["plant"],
            "on_time": str(r["delivered_on_time"]).lower() == "true"} for r in _rows(md / "po_history.csv")]
    contracts: dict[str, Contract] = {}
    for r in _rows(cd / "contract_register.csv"):
        contracts[r["contract_id"]] = Contract(r["contract_id"], r["supplier_id"], d(r["start_date"]), d(r["end_date"]),
                                               float(r["value_cap"]), float(r["consumed_value"]))
    for r in _rows(cd / "contract_lines.csv"):
        contracts[r["contract_id"]].lines[r["item_code"]] = float(r["rate"])
    return Master(items, suppliers, plants, pos, list(contracts.values()))
