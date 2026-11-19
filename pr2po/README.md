# Corvo PR → PO (policy-driven)

Chat intake → decision engine driven by a **versioned policy stored as data** → buyer workspace → POs / RFQs.

## Run locally
```bash
docker compose up --build          # http://localhost:8000
# or without Docker
pip install -r requirements.txt && uvicorn app.main:app --port 8000
pytest -q                          # 20 tests
```
Optional: `export ANTHROPIC_API_KEY=...` to enable the AI policy reader and chat extraction (works without it).

## Deploy (Render, free)
Push this repo to GitHub → Render → New Web Service → pick the repo (it has a `Dockerfile` and `render.yaml`). Add `ANTHROPIC_API_KEY` if you want AI. SQLite lives in `/srv/data` (use a disk to persist, or it resets on redeploy).

## Key API
| | |
|--|--|
| `POST /api/process-batch` | PR batch CSV (multipart `file` or raw body) → decisions JSON; `?format=csv` for the section-4 CSV; `?persist=false` to dry run; `?policy_version=5.0` to pin |
| `GET /api/runs/{id}/decisions.csv` | export a stored run |
| `POST /api/runs/{id}/reprocess` | same lines under the currently approved policy (new run) |
| `GET /api/preferred-suppliers.csv?as_of=YYYY-MM-DD&top=3` | `item_code,rank,supplier_id,reference_price` |
| `POST /api/policies/propose` → `/approve` | upload/describe a change → diff → approve |
| `POST /api/contracts/extract`, `POST /api/contracts` | PDF-only contracts |

## Blind-test runbook
1. **Run 1:** Workspace → upload batch (policy v5.0) → *Export decisions CSV*.
2. **Policy tab** (role *procurement*): upload the amendment → check the plain-language diff and impact → *Approve*.
3. **Run 2:** Workspace → *Reprocess under current policy* → export.
4. Preferred lists: `/api/preferred-suppliers.csv?as_of=<date given>`.
5. Put the three CSVs in `submission/` and push before 12:00.

## Layout
`app/engine/` pure decision engine (policy interpreter, matching, ranking, pipeline) · `app/service.py` workflow + storage · `app/main.py` API · `app/chat.py` intake · `app/policy_update.py` amendment reader · `policies/` policy v5.0 JSON · `seed/` kit data · `DECISIONS.md`, `APPROACH.md`.
