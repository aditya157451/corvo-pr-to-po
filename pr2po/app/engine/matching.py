"""Item matching (policy 1.1/1.2). Deterministic, never invents an item: the result is always
an item from the master or NEW. Used by the batch pipeline and the chat assistant."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

ABBREV = {
    "brg": "bearing", "brgs": "bearing", "hyd": "hydraulic", "pneu": "pneumatic", "pnu": "pneumatic",
    "rod": "electrode", "elec": "electrode", "cyl": "cylinder", "pcs": "", "pc": "", "nos": "",
    "ltrs": "ltr", "mtrs": "mtr", "kgs": "kg", "inches": "inch",
    "monitors": "monitor", "tpi": "", "mm.": "mm",
}
NOISE = {
    "for", "the", "a", "an", "of", "as", "per", "sample", "urgent", "urgently", "asap", "repeat", "need", "needed",
    "required", "require", "pls", "please", "maintenance", "immediately", "new", "stock", "to", "and", "with",
    "we", "our", "plant", "reorder", "again", "emergency", "breakdown", "spare", "spares",
}
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def normalize(text: str) -> list[str]:
    t = (text or "").lower().replace("×", "x")
    t = re.sub(r"(\d)\s*(?:\"|”|in\b|inches\b)", r"\1 inch", t)
    t = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "x", t)            # 10 x 40 -> 10x40
    toks = []
    for tok in _TOKEN.findall(t):
        tok = ABBREV.get(tok, tok)
        if not tok or tok in NOISE:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") and not any(c.isdigit() for c in tok):
            tok = tok[:-1]
        toks.append(tok)
    return toks


def _has_digit(tok: str) -> bool:
    return any(c.isdigit() for c in tok)


def _weight(tok: str) -> float:
    return 2.0 if _has_digit(tok) and len(tok) > 1 else 1.0


def _tok_match(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if _has_digit(a) or _has_digit(b):
        return 0.0
    if len(a) >= 5 and len(b) >= 5:
        r = difflib.SequenceMatcher(None, a, b).ratio()
        if r >= 0.84:
            return r
    return 0.0


@dataclass
class Candidate:
    item_code: str
    description: str
    score: float
    coverage: float
    precision: float


@dataclass
class MatchResult:
    item_code: str | None             # None => NEW
    method: str                       # code | text | new | ambiguous
    confidence: float
    candidates: list[Candidate] = field(default_factory=list)
    note: str = ""


class ItemMatcher:
    def __init__(self, items: dict[str, dict]):
        self.items = items
        self._tok = {c: normalize(i["description"]) for c, i in items.items()}

    def candidates(self, text: str, limit: int = 5, min_cov: float = 0.25) -> list[Candidate]:
        req = normalize(text)
        if not req:
            return []
        out = []
        for code, itoks in self._tok.items():
            if not itoks:
                continue
            tw = sum(_weight(t) for t in itoks)
            got = 0.0
            used = set()
            for t in itoks:
                best, bj = 0.0, None
                for j, r in enumerate(req):
                    if j in used:
                        continue
                    m = _tok_match(t, r)
                    if m > best:
                        best, bj = m, j
                if bj is not None and best > 0:
                    used.add(bj)
                    got += _weight(t) * best
            cov = got / tw
            if cov < min_cov:
                continue
            rw = sum(_weight(r) for r in req)
            prec = sum(_weight(req[j]) for j in used) / rw if rw else 0.0
            out.append(Candidate(code, self.items[code]["description"], round(0.65 * cov + 0.35 * prec, 4),
                                 round(cov, 4), round(prec, 4)))
        out.sort(key=lambda c: (-c.score, c.item_code))
        return out[:limit]

    def match(self, item_code: str | None, description: str) -> MatchResult:
        code = (item_code or "").strip().upper()
        if code and code in self.items:
            return MatchResult(code, "code", 1.0)
        note = f"item code '{item_code}' not in item master; matched on description" if code else ""
        cands = self.candidates(description)
        if not cands:
            return MatchResult(None, "new", 0.0, [], note)
        best = cands[0]
        second = cands[1].score if len(cands) > 1 else 0.0
        itoks = self._tok[best.item_code]
        digit_w = sum(_weight(t) for t in itoks if _has_digit(t))
        req = normalize(description)
        digit_hit = sum(_weight(t) for t in itoks if _has_digit(t) and any(_tok_match(t, r) for r in req))
        digits_ok = digit_w == 0 or digit_hit / digit_w >= 0.99
        strong = best.coverage >= 0.80 and best.precision >= 0.60 and digits_ok
        likely = best.coverage >= 0.50 and best.precision >= 0.99 and digits_ok and (best.score - second) >= 0.25
        if (strong or likely) and (best.score - second) >= 0.08:
            return MatchResult(best.item_code, "text", best.score, cands, note)
        near = f"closest was {best.item_code} '{best.description}' ({best.score:.0%}), not confident enough"
        return MatchResult(None, "new" if best.score < 0.6 else "ambiguous", best.score, cands,
                           (note + "; " if note else "") + near)
