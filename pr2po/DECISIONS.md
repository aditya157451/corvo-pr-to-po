# DECISIONS.md

Where the policy, brief or data was silent or ambiguous: what I noticed, what I decided, why.

## Data and input handling
1. **Plant `PUN2` (PR-26-00173)** is not a Corvo plant. I did not guess "PUN1". Route `NOT_PROCESSED`, reason given; a human fixes the plant.
2. **Quantity 0 (PR-26-00172)** and non-numeric/negative quantities: `NOT_PROCESSED`. A zero-quantity line has no value and would pollute clubbing.
3. **Duplicate (pr_no, pr_line)**: first kept, later ones `NOT_PROCESSED`. Missing/invalid `created_at`: `NOT_PROCESSED` (clubbing and validity need it).
4. **"- urgent" in the description while the urgency field says Normal**: treated as **Normal**, flagged for the buyer. Emergency changes the route (5.3 skips quotes up to ₹5L), so a free-text word must not silently unlock it. The structured field wins.
5. **Rows appended at the end of the file with the earliest timestamps** (PR-174/175): the engine sorts by creation time, never file order. The worked examples' PO numbers are 1 lower than mine for the full sample batch; with PR-172..175 removed I reproduce all of them (PO-0001, 0002, 0003, 0011, 0023, 0024, 0072), so those four rows were evidently added after the examples were generated.
6. **Item code present but unknown**: fall back to the description; if that fails, NEW with a note.
7. **New item with no estimated price**: still `NEW_ITEM_SOURCING`, value left blank and flagged (cannot invent a value).

## Item matching (1.1 / 1.2)
8. Deterministic token matcher (abbreviation map such as brg→bearing, hyd, pneu, rod→electrode; noise words like "as per sample", "(repeat)"; typo tolerance for words; **numbers/specs must match exactly**). Accept only if confident and clearly ahead of the runner-up.
9. **Low confidence ⇒ NEW, never a guess.** Policy 1.2 says no match = new item. The reason names the closest item and its score so the buyer can correct it. In chat the assistant instead shows the candidates and asks.

## Clubbing (2.2)
10. "Within 14 calendar days" is compared on **dates** (creation date minus the requirement's first-line date); need-by is compared to the **first line's** need-by (±7 days). A line joins the earliest qualifying requirement. NEW items are never clubbed. Lines in a requirement may come from different PRs/requesters/departments.

## Valuation and contracts (3, 4)
11. Contract applicability uses the **requirement date** (first line's creation date), inclusive of both end dates, and the supplier must be Active. Contract **RC-2026-005 is held by a Blocked supplier (S004)** so it is never used or relied on.
12. **Ceiling:** requirements consume in creation order. Only requirements that actually end in a CONTRACT_PO consume. Register `consumed_value` is the starting point; PDF-only contracts start at 0. Consumption is **batch-scoped**: each run starts from the register, so reprocessing or a second run is repeatable and approvals do not double-count. (Trade-off: approvals in the app do not carry ceiling into later batches.)
13. **PDF-only contracts** (RC-2026-031, RC-2026-033) are extracted from their PDFs at startup by a deterministic parser. I verified the parser reproduces all 10 register contracts exactly (supplier, dates, ceiling, every rate) before trusting it. Buyers can add more via PDF upload (extract → review → save) or manually.
14. Valuation 3.1(b) uses the most recent PO in the 365 days before the requirement date from **any** supplier (even Blocked): it is a price signal, not a purchase.

## Preferred list and ranking (6)
15. **Score = 0.30·frequency + 0.25·recency + 0.20·contract + 0.15·on-time + 0.10·quality**, each 0..1:
    frequency = min(POs in 548 days, 6)/6; recency = 1 if last PO ≤90 days, 0.5 if ≤365, else 0 (same tiers as the worked examples); contract = 1 if the supplier has held a contract for the item that started on/before the date (even if expired); on-time = %/100; quality = rating/5. Ties: on-time, quality, supplier id.
    *Why:* frequency and recency are direct evidence we can buy from them; a contract is a negotiated commitment; delivery and quality matter but are supplier-wide, not item-specific, so they get less weight. I checked it against the three example lists: it reproduces all three orders.
16. **Weights live in the policy JSON** (`ranking`), so an amendment can change them. The UI shows each component per supplier.
17. **Reference price**: last PO price in 548 days, else most recent contract rate (6.3). Window is "on or before the date, on or after date−548".
18. **Top-up (6.4)**: other Active suppliers listing the category, ordered by the mean of on-time% and quality/5. I derived this from the examples (S017, S019, S018 and S047, S020, S048, S046 orders are only consistent with a blend, not on-time alone).
19. **Blocked/Inactive** are excluded at list-build time and a hard assertion before output makes it impossible to emit one (5.7). A buyer override that picks one is rejected.

## Decision rules and POs
20. Rule 5.3/5.4 "bought from an Active supplier in the last 365 days" = a PO from an Active supplier dated ≤ requirement date and within 365 days. Boundaries: ≤ ₹5,00,000 and < ₹2,00,000 / < ₹10,00,000 exactly as written.
21. If a rule matches but cannot be carried out (e.g. PO rule but no preferred supplier), the engine skips it, notes why, and continues down the list.
22. **PO consolidation**: PO numbers are assigned in order of the PO's first requirement creation time (matches the examples). Window is inclusive (≤ 24h from the first requirement on the PO).
23. Unit price is output with 2 decimals; values are quantity × exact unit price (the worked examples show rounded rupees).

## Policy as data
24. The whole policy (limits, windows, ranking, **ordered rules with generic conditions**) is JSON with version + effective date + status. The engine is a generic interpreter: conditions on `item_type, category, plant, value, is_emergency, contract_usable, days_since_active_purchase, …`. A new rule kind (e.g. plant-only) is just an `add_rule` operation: no code (see `test_plant_only_rule_is_pure_data`).
25. **Which version applies:** per requirement, the version in force on its **requirement date** (consistent with 2.3). A batch can therefore span two versions; the output `policy_version` column says which. A run can also be pinned to one version.
26. **Amendment flow:** document or chat text → reader (LLM if `ANTHROPIC_API_KEY` is set, otherwise a built-in reader for limits, windows and plant/category-scoped rules) → patch of operations → new draft version → plain-language diff + validation + **impact on the latest batch** → Procurement approves → active from its effective date. Nothing activates without approval; no effective date found ⇒ defaults to today and warns.
27. The built-in reader is deliberately limited; the AI reader handles free-form amendments. Both produce the same patch format, and the draft can be reviewed before approval.

## Workflow
28. Identity is a name + role header (requester/buyer/procurement); no passwords. Enough to audit who/when; not production security.
29. Approve creates the PO (consolidated lines) or RFQ/RFP/Capex event; Capex events are "Awaiting capex approval". Override needs a ≥5-char reason, keeps the original decision, and cannot choose an inactive supplier.
30. Chat PRs go to a **live queue** that is re-evaluated as a whole so later chat PRs club with earlier ones. If a new line joins an already-approved requirement it is re-opened and audited.

## Known limitations
- The hosted link and `docker compose` are set up in the repo, but I could not deploy from my environment: you must push and deploy (README).
- Unit-of-measure conversions are not handled (PR UoM always equals the item master UoM in the sample).
- Ceiling consumption is batch-scoped (item 12).
