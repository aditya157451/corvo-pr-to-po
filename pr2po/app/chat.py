"""Chat intake. A small state machine decides what to ask next; language understanding (slot extraction)
is done by the LLM when configured, otherwise by the built-in parser. Item resolution is ALWAYS the
deterministic matcher: the assistant can only pick real item-master items or flag a new item, never invent one."""
from __future__ import annotations

import datetime as dt
import re
import uuid

from . import llm
from .db import jd, jl
from .engine.matching import ItemMatcher

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PLANT_WORDS = {"chk1": "CHK1", "chakan": "CHK1", "pun1": "PUN1", "bhosari": "PUN1", "pune": "PUN1"}
UNITS = r"(?:nos|no|pcs|pc|pieces|piece|pairs|pair|kg|kgs|ltr|ltrs|litres|liters|litre|liter|mtr|mtrs|meters|metres|meter|sets|set|rolls|roll|reams|ream|jobs|job|cyl|cylinders|nos\.)"
URGENT_RE = re.compile(r"\b(urgent|urgently|asap|emergency|immediately|breakdown|line stop|production stop\w*)\b", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
YES = {"yes", "y", "ok", "okay", "confirm", "correct", "right", "yep", "sure", "go ahead", "looks good", "confirmed"}
NO = {"no", "n", "nope", "cancel", "wrong", "stop"}


# --------------------------------------------------------------------------- built-in understanding
def parse_date(text: str, today: dt.date) -> tuple[dt.date | None, str | None]:
    """Returns (date, matched phrase)."""
    t = text.lower()
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3])), m[0]
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if m:
        y = int(m[3]) if m[3] else today.year
        y += 2000 if y < 100 else 0
        try:
            d = dt.date(y, int(m[2]), int(m[1]))
            if not m[3] and d < today:
                d = d.replace(year=y + 1)
            return d, m[0]
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(20\d{2}))?\b", t) or \
        re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", t)
    if m:
        a, b, y = m.group(1), m.group(2), m.group(3)
        day, mon = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
        yr = int(y) if y else today.year
        try:
            d = dt.date(yr, mon, day)
            if not y and d < today:
                d = d.replace(year=yr + 1)
            return d, m[0]
        except ValueError:
            pass
    m = re.search(r"\bin\s+(\d+)\s+(day|days|week|weeks)\b", t)
    if m:
        return today + dt.timedelta(days=int(m[1]) * (7 if m[2].startswith("week") else 1)), m[0]
    if re.search(r"\bday after tomorrow\b", t):
        return today + dt.timedelta(days=2), "day after tomorrow"
    if re.search(r"\btomorrow\b", t):
        return today + dt.timedelta(days=1), "tomorrow"
    if re.search(r"\btoday\b", t):
        return today, "today"
    if re.search(r"\bnext week\b", t):
        return today + dt.timedelta(days=7 - today.weekday()), "next week"
    if re.search(r"\b(end of (?:this )?month|month end)\b", t):
        nxt = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return nxt - dt.timedelta(days=1), "end of month"
    m = re.search(r"\b(next|this|coming)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        wd = WEEKDAYS.index(m[2])
        if m[1] == "next":
            nxt_mon = today + dt.timedelta(days=7 - today.weekday())
            return nxt_mon + dt.timedelta(days=wd), m[0].strip()
        delta = (wd - today.weekday()) % 7
        return today + dt.timedelta(days=delta or 7), m[0].strip()
    return None, None


def parse_message(text: str, today: dt.date, awaiting: str | None = None) -> dict:
    """Built-in slot extraction. Returns {items:[{phrase,quantity,uom,est_price}], plant, need_by, urgency, urgent_signal, intent}."""
    raw = text.strip()
    low = raw.lower()
    out: dict = {"items": [], "plant": None, "need_by": None, "urgency": None, "urgent_signal": bool(URGENT_RE.search(raw)), "intent": None}
    if low in YES:
        out["intent"] = "yes"
    elif low in NO:
        out["intent"] = "no"
    if re.search(r"\b(cancel|start over|reset|never mind)\b", low):
        out["intent"] = "cancel"
    pl = next((v for k, v in PLANT_WORDS.items() if re.search(rf"\b{k}\b", low)), None)
    out["plant"] = pl
    if re.search(r"\b(emergency)\b", low) and not re.search(r"not (an )?emergency", low):
        out["urgency"] = "Emergency"
    if re.search(r"\b(normal|routine|not urgent|regular)\b", low):
        out["urgency"] = "Normal"
    d, phrase = parse_date(low, today)
    out["need_by"] = d.isoformat() if d else None
    # strip what we understood and read quantity + item from the remainder
    rest = raw
    for ph in (phrase,):
        if ph:
            rest = re.sub(rf"(?:\b(?:by|before|on|until|latest by|need by|needed by|delivered by|deliver by|due)\s+)?(?:the\s+)?{re.escape(ph)}", " ", rest, flags=re.I)
    rest = re.sub(r"\b(?:for|at|in|to|from)\s+(?:the\s+)?(?:chakan|bhosari|pune|chk1|pun1)(?:\s+plant)?\b", " ", rest, flags=re.I)
    rest = re.sub(r"\b(?:chakan|bhosari|chk1|pun1)(?:\s+plant)?\b", " ", rest, flags=re.I)
    rest = URGENT_RE.sub(" ", rest)
    rest = re.sub(r"\b(please|pls|kindly|hi|hello|hey|thanks|thank you)\b", " ", rest, flags=re.I)
    rest = re.sub(r"\b(i|we)\s+(?:need|require|want|would like|have to buy|should order)\b|\b(?:need|needs|require|want|order|buy|purchase|get|raise a pr for|raise pr for|pr for|create pr for|request for|requirement of|requirement for)\b", " ", rest, flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.;:-")
    parts = [p.strip(" ,.;:-") for p in re.split(r"\s*(?:;|,\s*(?=\d)|\band\b(?=\s+\d))\s*", rest) if p.strip(" ,.;:-")]
    for part in parts:
        mm = re.match(rf"^(\d[\d,]*(?:\.\d+)?)\s*(?:x\s+)?({UNITS}\b\.?)?\s*(?:of\s+)?(.*)$", part, re.I)
        if mm and mm.group(3) and not re.match(r"^(?:x\d|[x×]\s*\d|inch|mm\b|\")", mm.group(3), re.I):
            q = float(mm.group(1).replace(",", ""))
            out["items"].append({"phrase": mm.group(3).strip(" ,.-"), "quantity": q, "uom": (mm.group(2) or "").upper().strip("."), "est_price": None})
        elif mm and not mm.group(3):
            out["items"].append({"phrase": "", "quantity": float(mm.group(1).replace(",", "")), "uom": (mm.group(2) or "").upper().strip("."), "est_price": None})
        elif part and awaiting not in ("quantity", "plant", "need_by", "est_price"):
            tail = re.match(rf"^(.*?)\s+(\d[\d,]*)\s*({UNITS})?$", part, re.I)
            if tail and not re.search(r"\d", tail.group(1)):
                out["items"].append({"phrase": tail.group(1).strip(), "quantity": float(tail.group(2).replace(",", "")), "uom": (tail.group(3) or "").upper(), "est_price": None})
            else:
                out["items"].append({"phrase": part, "quantity": None, "uom": "", "est_price": None})
    em = re.search(r"(?:est(?:imated)?(?:\s+(?:unit\s+)?price)?|at|@|price|rate)\s*(?:of\s*)?(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)", low)
    if em and out["items"]:
        out["items"][-1]["est_price"] = float(em.group(1).replace(",", ""))
    return out


def llm_parse(text: str, state: dict, today: dt.date) -> dict:
    system = ("You extract purchase-requisition details from a requester's chat message for a manufacturing company. Output ONLY JSON: "
              '{"items":[{"phrase":"item as the requester wrote it, without quantity","quantity":number|null,"uom":string|"","est_price":number|null}],'
              '"plant":"PUN1"|"CHK1"|null,"need_by":"YYYY-MM-DD"|null,"urgency":"Emergency"|"Normal"|null,"urgent_signal":boolean,"intent":"yes"|"no"|"cancel"|null}. '
              "Bhosari/Pune = PUN1, Chakan = CHK1. Never invent items, quantities, plants or dates: use null when not stated. "
              "Do not put item specs (like 6205 or M10x40 or 1/2 inch) into quantity. urgent_signal is true if the message sounds urgent. "
              f"Today is {today.isoformat()} ({today.strftime('%A')}). The assistant is currently waiting for: {state.get('awaiting') or 'a new request'}.")
    return llm.complete_json(system, text, max_tokens=800, retries=0)


# --------------------------------------------------------------------------- bot
class ChatBot:
    def __init__(self, svc):
        self.svc = svc

    # ---- session persistence
    def _load(self, sid: str | None, user: dict) -> tuple[str, dict, list]:
        if sid:
            r = self.svc.db.one("SELECT * FROM chat_sessions WHERE id=?", (sid,))
            if r:
                return sid, jl(r["state"]), jl(r["messages"], [])
        return sid or uuid.uuid4().hex[:12], self._fresh(), []

    @staticmethod
    def _fresh() -> dict:
        return {"items": [], "plant": None, "need_by": None, "urgency": None, "urgent_signal": False, "awaiting": None, "focus": None, "done": False}

    def _save(self, sid: str, user: dict, state: dict, msgs: list):
        n = dt.datetime.now().isoformat(sep=" ", timespec="seconds")
        self.svc.db.x("INSERT INTO chat_sessions(id,user,state,messages,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, messages=excluded.messages, updated_at=excluded.updated_at",
                      (sid, user["name"], jd(state), jd(msgs[-60:]), n, n))

    # ---- main entry
    def handle(self, sid: str | None, user: dict, text: str = "", choice: str | None = None, today: dt.date | None = None) -> dict:
        today = today or dt.date.today()
        sid, st, msgs = self._load(sid, user)
        if st.get("done"):
            st = self._fresh()
        matcher = ItemMatcher(self.svc.master().items)
        label = text or (f"[{choice}]" if choice else "")
        msgs.append({"role": "user", "text": label})
        reply = self._step(st, text, choice, today, matcher, user)
        msgs.append({"role": "assistant", "text": reply["reply"]})
        self._save(sid, user, st, msgs)
        reply["session_id"] = sid
        reply["draft"] = self._draft_view(st)
        return reply

    # ---- state machine
    def _step(self, st: dict, text: str, choice: str | None, today: dt.date, matcher: ItemMatcher, user: dict) -> dict:
        m = self.svc.master()
        if choice:
            kind, _, val = choice.partition(":")
            if kind == "cancel":
                st.update(self._fresh())
                return self._say("No problem, I cancelled that request. What do you need?", [])
            if kind == "item":
                i = st["focus"] if st["focus"] is not None else 0
                if val == "NEW":
                    st["items"][i]["new"] = True
                    st["items"][i]["code"] = None
                    st["items"][i]["candidates"] = []
                else:
                    st["items"][i]["code"], st["items"][i]["new"] = val, False
                    st["items"][i]["candidates"] = []
                    st["items"][i]["need_item_confirm"] = False
                st["awaiting"] = None
            elif kind == "describe":
                st["awaiting"] = "item_text"
                return self._say("Sure. Describe the item the way it would appear in a catalogue (type, size, grade, make), for example 'ball bearing 6205-2RS'.", [])
            elif kind == "plant":
                st["plant"], st["awaiting"] = val, None
            elif kind == "urgency":
                st["urgency"], st["awaiting"] = val, None
            elif kind == "skip_price":
                i = st["focus"] if st["focus"] is not None else 0
                st["items"][i]["est_price_skipped"] = True
                st["awaiting"] = None
            elif kind == "need_by":
                st["need_by"], st["awaiting"] = val, None
            elif kind == "change":
                return self._say("What would you like to change? You can type e.g. 'quantity 300', 'plant Chakan', 'need by 30 Nov', 'make it an emergency'.", [], awaiting="change")
            elif kind == "confirm":
                return self._create(st, user)
        else:
            self._absorb(st, text, today, matcher)
            if st.get("_cancel"):
                st.pop("_cancel")
                st.update(self._fresh())
                return self._say("Cancelled. What do you need?", [])
            if st.get("_confirm") and self._complete(st) is None:
                st.pop("_confirm")
                return self._create(st, user)
            st.pop("_confirm", None)
        return self._next_question(st, today, m)

    def _absorb(self, st: dict, text: str, today: dt.date, matcher: ItemMatcher):
        awaiting = st.get("awaiting")
        try:
            p = llm_parse(text, st, today) if llm.available() else parse_message(text, today, awaiting)
        except Exception:
            p = parse_message(text, today, awaiting)
        if p.get("intent") == "cancel":
            st["_cancel"] = True
            return
        if p.get("intent") == "yes" and st.get("awaiting") == "confirm":
            st["_confirm"] = True
            return
        if p.get("intent") == "no" and st.get("awaiting") == "confirm":
            st["awaiting"] = "change"
            return
        if p.get("plant"):
            st["plant"] = p["plant"]
        if p.get("need_by"):
            st["need_by"] = p["need_by"]
        if p.get("urgency"):
            st["urgency"] = p["urgency"]
        if p.get("urgent_signal"):
            st["urgent_signal"] = True
        items = [i for i in (p.get("items") or []) if i.get("phrase") or i.get("quantity")]
        # answers to a specific question
        if awaiting == "quantity" and items:
            i = st["focus"] if st["focus"] is not None else 0
            q = next((x["quantity"] for x in items if x.get("quantity")), None)
            if q:
                st["items"][i]["quantity"] = q
                if next((x.get("uom") for x in items if x.get("uom")), None):
                    st["items"][i]["uom"] = next(x["uom"] for x in items if x.get("uom"))
                items = []
        elif awaiting == "est_price":
            nums = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
            if nums and st["focus"] is not None:
                st["items"][st["focus"]]["est_price"] = float(nums[0].replace(",", ""))
                items = []
        elif awaiting == "item_text" and text.strip():
            i = st["focus"] if st["focus"] is not None else 0
            st["items"][i]["phrase"] = re.sub(r"^\d+\s*", "", text.strip())
            self._resolve(st["items"][i], matcher)
            items = []
        elif awaiting == "change":
            if items and st["items"] and len(items) == 1 and items[0].get("quantity") and not items[0].get("phrase"):
                st["items"][0]["quantity"] = items[0]["quantity"]
                items = []
            m = re.search(r"(?:quantity|qty)\s*(?:to|=|:)?\s*(\d[\d,]*)", text, re.I)
            if m and st["items"]:
                st["items"][0]["quantity"] = float(m.group(1).replace(",", ""))
                items = []
        elif awaiting in ("quantity", "plant", "need_by", "confirm") and items and not any(i.get("quantity") for i in items) and st["items"]:
            items = []                                              # a stray word is not a new item while we wait for something else
        for it in items:
            if st["items"] and st["items"][-1].get("phrase") == "" and it.get("phrase"):
                st["items"][-1]["phrase"] = it["phrase"]
                st["items"][-1]["quantity"] = st["items"][-1]["quantity"] or it.get("quantity")
                self._resolve(st["items"][-1], matcher)
                continue
            new = {"phrase": it.get("phrase", ""), "quantity": it.get("quantity"), "uom": it.get("uom", ""), "est_price": it.get("est_price"), "code": None, "new": False, "candidates": [], "conf": 0.0, "resolved": False}
            st["items"].append(new)
            if new["phrase"]:
                self._resolve(new, matcher)

    @staticmethod
    def _resolve(it: dict, matcher: ItemMatcher):
        res = matcher.match(None, it["phrase"])
        it["resolved"] = False
        it["candidates"] = [{"item_code": c.item_code, "description": c.description, "score": c.score} for c in res.candidates[:4]]
        if res.item_code:
            it["code"], it["conf"], it["new"] = res.item_code, res.confidence, False
            it["resolved"] = res.confidence >= 0.85
            it["need_item_confirm"] = res.confidence < 0.85
        else:
            it["code"], it["conf"] = None, res.confidence

    # ---- what is still missing?
    def _complete(self, st: dict):
        """Returns the first missing thing as (kind, index) or None."""
        for i, it in enumerate(st["items"]):
            if not it.get("phrase") and not it.get("code"):
                return ("item_text", i)
            if not it.get("code") and not it.get("new"):
                return ("item", i)
            if it.get("need_item_confirm") and it.get("code"):
                return ("item_confirm", i)
            if not it.get("quantity"):
                return ("quantity", i)
            if it.get("new") and it.get("est_price") is None and not it.get("est_price_skipped"):
                return ("est_price", i)
        if not st["items"]:
            return ("start", None)
        if not st.get("plant"):
            return ("plant", None)
        if not st.get("need_by"):
            return ("need_by", None)
        if st.get("urgent_signal") and not st.get("urgency"):
            return ("urgency", None)
        return None

    def _next_question(self, st: dict, today: dt.date, m) -> dict:
        miss = self._complete(st)
        if miss is None:
            st["urgency"] = st.get("urgency") or "Normal"
            st["awaiting"] = "confirm"
            return self._say(self._confirm_text(st, m), [("Confirm and create PR", "confirm"), ("Change something", "change"), ("Cancel", "cancel")], awaiting="confirm")
        kind, i = miss
        st["focus"] = i
        if kind == "start":
            return self._say("Hi! Tell me what you need, for example: 'need 200 nitrile gloves for Chakan by next Friday'.", [], awaiting=None)
        it = st["items"][i] if i is not None else None
        if kind == "item_text":
            return self._say("Which item do you need? Please describe it (type, size, grade).", [], awaiting="item_text")
        if kind == "item_confirm":
            d = m.items[it["code"]]["description"]
            return self._say(f"I understood '{it['phrase']}' as **{it['code']} · {d}**. Is that right?", [("Yes, that item", f"item:{it['code']}")] + [(c["description"], f"item:{c['item_code']}") for c in it["candidates"] if c["item_code"] != it["code"]][:2] + [("It's a new item", "item:NEW")], awaiting="item")
        if kind == "item":
            cands = it["candidates"]
            if cands:
                chips = [(f"{c['item_code']} · {c['description']}", f"item:{c['item_code']}") for c in cands[:4]]
                return self._say(f"I couldn't match '{it['phrase']}' to one item with confidence. Is it one of these? I won't guess: pick one, or tell me it's a new item.",
                                 chips + [("None of these, it's a new item", "item:NEW"), ("Let me describe it again", "describe:")], awaiting="item")
            return self._say(f"I couldn't find anything like '{it['phrase']}' in the item master. Is it a new item that Procurement should source, or would you like to describe it differently?",
                             [("It's a new item", "item:NEW"), ("Let me describe it again", "describe:")], awaiting="item")
        desc = (m.items[it["code"]]["description"] if it and it.get("code") else (it or {}).get("phrase", "the item"))
        if kind == "quantity":
            uom = m.items[it["code"]]["uom"] if it.get("code") else ""
            return self._say(f"How many {desc}{' (' + uom + ')' if uom else ''} do you need?", [], awaiting="quantity")
        if kind == "est_price":
            return self._say(f"'{it['phrase']}' is not in the item master, so I'll raise it as a new item for sourcing. What is your estimated price per unit (₹)? Procurement uses it to value the request.",
                             [("I don't know, skip", "skip_price:")], awaiting="est_price")
        if kind == "plant":
            return self._say("Which plant is this for?", [("Bhosari (PUN1)", "plant:PUN1"), ("Chakan (CHK1)", "plant:CHK1")], awaiting="plant")
        if kind == "need_by":
            def f(n): return (today + dt.timedelta(days=n)).isoformat()
            return self._say("By when do you need it? Type a date (e.g. '30 Nov' or 'next Friday') or pick one.",
                             [(f"In 7 days ({(today + dt.timedelta(days=7)).strftime('%d %b')})", f"need_by:{f(7)}"), (f"In 14 days ({(today + dt.timedelta(days=14)).strftime('%d %b')})", f"need_by:{f(14)}"),
                              (f"In 30 days ({(today + dt.timedelta(days=30)).strftime('%d %b')})", f"need_by:{f(30)}")], awaiting="need_by")
        if kind == "urgency":
            return self._say("You sounded urgent. Is this an **Emergency** (production or safety at risk)? Emergencies can be ordered faster but are reviewed closely.",
                             [("Yes, Emergency", "urgency:Emergency"), ("No, Normal", "urgency:Normal")], awaiting="urgency")
        return self._say("Could you tell me more?", [])

    def _confirm_text(self, st: dict, m) -> str:
        lines = []
        for it in st["items"]:
            if it.get("code"):
                lines.append(f"• {int(it['quantity']) if float(it['quantity']).is_integer() else it['quantity']} × **{it['code']} · {m.items[it['code']]['description']}**")
            else:
                est = f" (estimate ₹{it['est_price']:,.0f}/unit)" if it.get("est_price") else " (no estimate)"
                lines.append(f"• {int(it['quantity']) if float(it['quantity']).is_integer() else it['quantity']} × **{it['phrase']}**: NEW item{est}")
        nb = dt.date.fromisoformat(st["need_by"])
        return ("Please check your request:\n" + "\n".join(lines) +
                f"\nPlant: **{st['plant']}**  ·  Need by: **{nb.strftime('%a %d %b %Y')}**  ·  Urgency: **{st['urgency'] or 'Normal'}**\nShall I create the PR?")

    def _create(self, st: dict, user: dict) -> dict:
        miss = self._complete(st)
        if miss is not None:
            return self._next_question(st, dt.date.today(), self.svc.master())
        m = self.svc.master()
        pr_no = self.svc.next_pr_no()
        now = dt.datetime.now().replace(second=0, microsecond=0)
        lines = []
        for n, it in enumerate(st["items"], 1):
            uom = m.items[it["code"]]["uom"] if it.get("code") else (it.get("uom") or "NOS")
            lines.append({"pr_no": pr_no, "pr_line": n, "created_at": now.strftime("%Y-%m-%d %H:%M"), "requester": user["name"], "department": "",
                          "plant": st["plant"], "item_code": it.get("code") or "", "description": m.items[it["code"]]["description"] if it.get("code") else it["phrase"],
                          "quantity": it["quantity"], "uom": uom, "estimated_unit_price": it.get("est_price") or "", "need_by_date": st["need_by"], "urgency": st["urgency"] or "Normal"})
        info = self.svc.add_live_lines(lines, user)
        self.svc.audit(user, "create_pr", "pr", pr_no, {"lines": len(lines), "plant": st["plant"], "via": "chat"})
        # preview of what Procurement will see
        rows = [r for r in self.svc.run_rows(1) if r["pr_no"] == pr_no]
        prev = []
        for r in rows:
            who = r["supplier_id"] or (r["suggested_supplier_ids"] or "buyer to find suppliers")
            prev.append(f"• line {r['pr_line']}: {r['route']} ({who}), requirement value ₹{float(r['group_value_inr']):,.0f}" if r["group_value_inr"] != "" else f"• line {r['pr_line']}: {r['route']}")
        st["done"] = True
        st["awaiting"] = None
        reply = self._say(f"✅ **{pr_no}** created with {len(lines)} line(s). Procurement's policy check says:\n" + "\n".join(prev) +
                          "\nA buyer will review and approve it. You can start another request any time.", [("Raise another PR", "reset:")])
        reply["created"] = {"pr_no": pr_no, "run_id": 1, "rows": rows}
        return reply

    # ---- helpers
    @staticmethod
    def _say(text: str, chips: list[tuple[str, str]], awaiting: str | None = None) -> dict:
        return {"reply": text, "quick_replies": [{"label": a, "value": b} for a, b in chips], "awaiting": awaiting}

    def _draft_view(self, st: dict) -> dict:
        m = self.svc.master()
        return {"plant": st.get("plant"), "need_by": st.get("need_by"), "urgency": st.get("urgency"),
                "items": [{"phrase": i.get("phrase"), "code": i.get("code"), "description": m.items[i["code"]]["description"] if i.get("code") else None,
                           "quantity": i.get("quantity"), "new": bool(i.get("new")), "est_price": i.get("est_price")} for i in st["items"]]}
