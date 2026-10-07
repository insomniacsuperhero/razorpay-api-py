# Proof Bridge API (razorpay-api-py)

FastAPI backend for Proof Bridge, built from the Backend Implementation Guide. It turns phone evidence into AI-extracted facts, checks them against the real Razorpay test payment and fixed rules, uploads useful files to Razorpay Documents (real `doc_` IDs), and contests or accepts through the dispute provider (the live mock now, real Razorpay later).

```
app/
  main.py               FastAPI app, CORS, error contract, /health
  config.py             settings; strict checks when APP_ENV=production
  api/v1/routes.py      every route in Guide §9 (+ demo and dev-storage routes)
  clients/              razorpay.py · disputes.py (mock/real provider) · storage.py (R2/local) · ai.py (Gemini/fake)
  domain/               evidence_rules.py · matcher.py · coverage.py · summary.py   (deterministic, no AI)
  services/             files.py (type sniffing, image/PDF/DOCX normalising) · evidence.py (pipeline + cards)
                        disputes.py · uploads.py · webhooks.py · context.py
  security/             tokens.py (deep link → access token) · hmac_verify.py · idempotency.py
  db/                   models.py (Appendix B + idempotency_keys) · session.py
migrations/             Alembic (0001_initial_schema)
sql/schema.sql          same schema as plain SQL for the Supabase SQL editor
scripts/check_integrations.py   contract spike against your real accounts
tests/                  33 tests; Razorpay + mock replaced by in-memory fakes
```

## Run locally (no accounts needed)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # defaults: SQLite, local storage, fake AI
pytest -q                       # 33 passed
uvicorn app.main:app --reload --port 8000
open http://localhost:8000/docs
```

Add your `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` to `.env` to use the real test payment and Documents API and the live mock. Then:

```bash
python scripts/check_integrations.py pay_TkYxHTO8GSVwg5    # read-only/test-only checks, never submits
```

## Try the whole flow with curl

```bash
API=http://localhost:8000/api/v1; ADMIN='X-Demo-Secret: <DEMO_ADMIN_SECRET>'
# 1. new mock dispute on a real captured test payment + alert link
curl -s -X POST $API/demo/seed -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"payment_id":"pay_TkYxHTO8GSVwg5","reason_code":"goods_not_received","respond_in_hours":72}'
# 2. exchange the link token (single use) for an access token
curl -s -X POST $API/sessions/exchange -H 'Content-Type: application/json' -d '{"token":"<token>"}'
AUTH='Authorization: Bearer <access_token>'; D=<dispute_id>
# 3. upload: get a signed URL, PUT the file to it, complete
curl -s -X POST $API/disputes/$D/uploads -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"filename":"delhivery.jpg","mime_type":"image/jpeg","size_bytes":123456}'
curl -s -X PUT "<put_url>" -H 'Content-Type: image/jpeg' --data-binary @delhivery.jpg
curl -s -X POST $API/disputes/$D/uploads/<upload_id>/complete -H "$AUTH"
# 4. poll the card until status=ready (verdict, facts, razorpay_document_id)
curl -s $API/disputes/$D/evidence/<evidence_id> -H "$AUTH"
# 5. coverage + summary, then submit (Idempotency-Key required)
curl -s $API/disputes/$D/coverage -H "$AUTH"
curl -s -X POST $API/disputes/$D/contest -H "$AUTH" -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' -d '{"action":"submit"}'
```

## Frontend contract (Guide §15.3)

1. Open `/d/:disputeId?t=<token>` → `POST /sessions/exchange` → keep `access_token` in memory.
2. `GET /disputes/:id` → screen 1 (dispute, masked payment, requirements, existing cards, `bank_request`).
3. Tap **Add proof** → `POST /disputes/:id/timer/start`.
4. Per file: `POST /uploads` → `PUT put_url` with the exact `Content-Type` → `POST /uploads/:upload_id/complete`.
5. Poll `GET /evidence/:evidence_id` every ~1 s. `stage_index` follows queued → extracting → analysing → validating → uploading → ready. Stop on `ready` or `failed`.
6. Fix a card with `PATCH /evidence/:id`, using any of: `{fact_key, value}`, `{final_field}`, `{confirm: true}`, `{answer: "yes"|"no"}` or `{clear_bank_flag: true}`. Remove a card with `DELETE`. Retry a failed card with `POST /evidence/:id/process`.
7. `GET /coverage` → checklist + `summary.draft`. `PATCH /draft` saves the seller's edits.
8. `POST /contest {action: draft|submit}` or `POST /accept`, each with a fresh `Idempotency-Key`.
9. Errors are always `{"error": {"code", "message"}}`. The codes are in `app/errors.py`, per Guide §17.2.

## Deploy to Render

1. Create a Supabase project, then **Connect → Session pooler**. Prefix the URL with `postgresql+psycopg://` and URL-encode the password. Run `alembic upgrade head` against it, or paste `sql/schema.sql` into the SQL editor.
2. Create an R2 bucket called `proof-bridge-evidence` (private) and an Object Read & Write token for it. Add this CORS rule:
   `[{"AllowedOrigins":["http://localhost:5173","https://<your-vercel-app>.vercel.app"],"AllowedMethods":["PUT","GET","HEAD"],"AllowedHeaders":["Content-Type"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3600}]`
3. Get a Gemini API key from Google AI Studio and set `GEMINI_MODEL` to the current Flash model. Use only synthetic or anonymised evidence on the free tier.
4. In Render, go to **New → Blueprint** and pick this repo. `render.yaml` sets the build and start commands, `/health` check and the generated secrets. Fill in the variables marked `sync: false`.
   - Manual alternative: Web Service (no root directory), build command `pip install -r requirements.txt`, start command `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
5. Set up webhooks:
   - **Razorpay test dashboard:** add a webhook at `https://<render>/api/v1/webhooks/razorpay` (events `payment_link.paid`, `payment.captured`), using the same secret as `RAZORPAY_WEBHOOK_SECRET`.
   - **Mock (Vercel env vars):** set `WEBHOOK_URL=https://<render>/api/v1/webhooks/disputes` and `WEBHOOK_SECRET` equal to `MOCK_DISPUTES_WEBHOOK_SECRET`, then redeploy the mock.
6. Before a demo, open `https://<render>/health` a minute early; Render's free tier sleeps after 15 minutes idle.

## Changes from the guide

- **No `python-magic`.** File types are detected from magic bytes in `services/files.py`, so Render doesn't need `libmagic`.
- **Extra columns and one extra table:**
  - columns: `document_type`, `readable`, `merchant_confirmed`, `bank_flag`, `verdict_reason`, `question`, `mismatches`, `processing_notes`, `error_code`, `removed_at`, `summary_edited`
  - table: `idempotency_keys` (for §13.2)
- **Processing runs as a FastAPI background task** after `/complete`, with no worker queue. That's enough for one demo merchant.
- **Local mode** (local storage, fake AI, SQLite) runs the whole flow without R2, Gemini or Supabase.
  - The fake extractor is deterministic; it reads text and filename keywords.
  - Use it only for development, tests or a clearly labelled demo fallback.
- **The mock is called with Basic auth** (the Razorpay test key and secret), because the deployed mock requires it.
- **Only `rzp_test_` keys are accepted**, and production settings are validated at startup.
