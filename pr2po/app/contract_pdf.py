"""Extract a rate contract from its signed PDF (deterministic text parsing, no LLM needed)."""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader


def pdf_text(src) -> str:
    reader = PdfReader(src)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def parse_contract_text(text: str) -> dict:
    flat = re.sub(r"\s+", " ", text)
    out: dict = {"warnings": []}

    def find(pat, label, flags=0):
        m = re.search(pat, flat, flags)
        if not m:
            out["warnings"].append(f"could not find {label}")
            return None
        return m

    m = find(r"Contract No\.?\s*(RC-\d{4}-\d{3,4})", "contract number")
    out["contract_id"] = m.group(1) if m else None
    m = find(r"Supplier ID\s*(S\d+)", "supplier id")
    out["supplier_id"] = m.group(1) if m else None
    m = find(r"valid from\s*(\d{4}-\d{2}-\d{2})\s*to\s*(\d{4}-\d{2}-\d{2})", "validity dates")
    out["start_date"], out["end_date"] = (m.group(1), m.group(2)) if m else (None, None)
    m = find(r"shall not exceed INR\s*([\d,]+(?:\.\d+)?)", "value ceiling")
    out["value_cap"] = float(m.group(1).replace(",", "")) if m else None
    lines = []
    for lm in re.finditer(r"(ITM-\d{4})\s+(.+?)\s+([A-Z]{2,4})\s+([\d,]+\.\d{2})", flat):
        lines.append({"item_code": lm.group(1), "description": lm.group(2).strip(), "uom": lm.group(3),
                      "rate": float(lm.group(4).replace(",", ""))})
    if not lines:
        out["warnings"].append("could not find any item rates in Schedule A")
    out["lines"] = lines
    return out


def parse_contract_pdf(src) -> dict:
    return parse_contract_text(pdf_text(src))


if __name__ == "__main__":  # quick CLI check
    import json, sys
    for p in sys.argv[1:]:
        print(json.dumps(parse_contract_pdf(Path(p)), indent=1))
