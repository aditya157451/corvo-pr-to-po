"""Seed loading: kit CSVs + any signed contract PDFs that are not in the register yet."""
from __future__ import annotations

from pathlib import Path

from .contract_pdf import parse_contract_pdf
from .engine.master import Contract, Master, d, load_seed


def contract_from_parsed(r: dict, source: str, consumed: float = 0.0) -> Contract:
    return Contract(r["contract_id"], r["supplier_id"], d(r["start_date"]), d(r["end_date"]), float(r["value_cap"]),
                    consumed, {l["item_code"]: float(l["rate"]) for l in r["lines"]}, source)


def load_full_seed(seed_dir: Path) -> Master:
    m = load_seed(seed_dir)
    known = {c.contract_id for c in m.contracts}
    for pdf in sorted((seed_dir / "contracts" / "pdf").glob("*.pdf")):
        r = parse_contract_pdf(pdf)
        if r["contract_id"] and r["contract_id"] not in known and not r["warnings"]:
            m.contracts.append(contract_from_parsed(r, "pdf", 0.0))   # signed after last register update: consumed = 0 (4.2)
            known.add(r["contract_id"])
    m.reindex()
    return m
