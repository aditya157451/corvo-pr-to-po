"""Preferred supplier list (policy section 6). Ranking weights live in the policy document
(`ranking`), so Procurement can change the method without code changes.

Score(supplier, item, as_of) = Σ weight_k × component_k, every component in 0..1:
  frequency : POs for the item in the lookback window, min(n, cap)/cap
  recency   : tiered on days since the last PO (policy tiers, default <=90d 1.0, <=365d 0.5, else 0)
  contract  : 1 if the supplier has held a contract covering the item that started on/before as_of
  on_time   : supplier on-time delivery % / 100
  quality   : supplier quality rating / 5
Ties: higher on_time, then higher quality, then supplier id.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .master import Master

DEFAULT_RANKING = {
    "weights": {"frequency": 0.30, "recency": 0.25, "contract": 0.20, "on_time": 0.15, "quality": 0.10},
    "frequency_cap": 6,
    "recency_tiers": [{"within_days": 90, "score": 1.0}, {"within_days": 365, "score": 0.5}, {"within_days": 100000, "score": 0.0}],
    "topup": {"on_time_weight": 0.5, "quality_weight": 0.5},
}


@dataclass
class Preferred:
    supplier_id: str
    rank: int = 0
    score: float = 0.0
    po_count: int = 0
    last_po_date: dt.date | None = None
    last_po_number: str | None = None
    last_po_price: float | None = None
    contract_ids: list[str] = field(default_factory=list)
    contract_rate: float | None = None
    contract_id: str | None = None          # most recent contract used for reference price
    reference_price: float | None = None
    reference_source: str = ""
    components: dict = field(default_factory=dict)
    recency: float = 0.0

    def as_dict(self) -> dict:
        return {
            "supplier_id": self.supplier_id, "rank": self.rank, "score": round(self.score, 4), "po_count": self.po_count,
            "last_po_date": self.last_po_date.isoformat() if self.last_po_date else None,
            "last_po_number": self.last_po_number, "reference_price": self.reference_price,
            "reference_source": self.reference_source, "contract_ids": self.contract_ids,
            "recency": self.recency, "components": self.components,
        }


def _recency(days: int | None, tiers: list[dict]) -> float:
    if days is None:
        return 0.0
    for t in sorted(tiers, key=lambda t: t["within_days"]):
        if days <= t["within_days"]:
            return float(t["score"])
    return 0.0


def preferred_list(master: Master, item: str, as_of: dt.date, lookback_days: int = 548,
                   ranking: dict | None = None) -> list[Preferred]:
    rk = ranking or DEFAULT_RANKING
    w = rk["weights"]
    cap = max(1, int(rk.get("frequency_cap", 6)))
    pos = master.pos_for(item, as_of, lookback_days)
    by_sup: dict[str, list[dict]] = {}
    for p in pos:
        by_sup.setdefault(p["supplier_id"], []).append(p)
    held: dict[str, list] = {}
    for c in master.contracts_by_item.get(item, []):
        if c.start <= as_of:
            held.setdefault(c.supplier_id, []).append(c)
    out: list[Preferred] = []
    for sid in sorted(set(by_sup) | set(held)):
        if not master.supplier_active(sid):
            continue                                   # 5.7: Blocked/Inactive never on the list
        s = master.suppliers[sid]
        p = Preferred(sid)
        sp = by_sup.get(sid, [])
        p.po_count = len(sp)
        if sp:
            last = sp[-1]                              # pos are oldest->newest
            p.last_po_date, p.last_po_number, p.last_po_price = last["po_date"], last["po_number"], last["unit_price"]
        cons = sorted(held.get(sid, []), key=lambda c: (c.start, c.contract_id))
        p.contract_ids = [c.contract_id for c in cons]
        if cons:
            p.contract_id = cons[-1].contract_id
            p.contract_rate = cons[-1].lines[item]
        # 6.3 reference price
        if p.last_po_price is not None:
            p.reference_price, p.reference_source = p.last_po_price, f"PO {p.last_po_number} ({p.last_po_date})"
        elif p.contract_rate is not None:
            p.reference_price, p.reference_source = p.contract_rate, f"contract {p.contract_id}"
        days = (as_of - p.last_po_date).days if p.last_po_date else None
        p.recency = _recency(days, rk["recency_tiers"])
        comp = {
            "frequency": min(p.po_count, cap) / cap,
            "recency": p.recency,
            "contract": 1.0 if cons else 0.0,
            "on_time": s["on_time_pct"] / 100.0,
            "quality": s["quality_rating"] / 5.0,
        }
        p.components = {k: round(v, 4) for k, v in comp.items()}
        p.score = sum(w.get(k, 0.0) * v for k, v in comp.items())
        out.append(p)
    out.sort(key=lambda p: (-round(p.score, 9), -master.suppliers[p.supplier_id]["on_time_pct"],
                            -master.suppliers[p.supplier_id]["quality_rating"], p.supplier_id))
    for i, p in enumerate(out, 1):
        p.rank = i
    return out


def category_topup(master: Master, category: str, exclude: set[str], ranking: dict | None = None) -> list[str]:
    """6.4: other Active suppliers listing the category, best delivery & quality record first."""
    tw = (ranking or DEFAULT_RANKING).get("topup", {"on_time_weight": .5, "quality_weight": .5})
    cands = []
    for sid, s in master.suppliers.items():
        if sid in exclude or not master.supplier_active(sid) or category not in s["categories"]:
            continue
        sc = tw["on_time_weight"] * s["on_time_pct"] / 100 + tw["quality_weight"] * s["quality_rating"] / 5
        cands.append((-round(sc, 9), sid))
    return [sid for _, sid in sorted(cands)]


def suggest_suppliers(master: Master, item: str, as_of: dt.date, count: int, lookback_days: int = 548,
                      ranking: dict | None = None) -> tuple[list[str], list[Preferred], list[str]]:
    """Returns (supplier ids, preferred list, topped-up ids)."""
    pl = preferred_list(master, item, as_of, lookback_days, ranking)
    ids = [p.supplier_id for p in pl][:count]
    topped: list[str] = []
    if len(ids) < count:
        cat = master.items[item]["category"]
        for sid in category_topup(master, cat, set(ids), ranking):
            if len(ids) >= count:
                break
            ids.append(sid)
            topped.append(sid)
    return ids, pl, topped
