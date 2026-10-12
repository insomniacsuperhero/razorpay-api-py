# Proof Bridge: API (razorpay-api-py)

The backend for Proof Bridge, a mobile flow that lets a seller answer a payment dispute from their phone in under 2 minutes. It:
- turns proof from the phone into facts, using AI to read each file;
- checks those facts against the real Razorpay test payment, using fixed rules;
- uploads useful files to Razorpay Documents (real `doc_` IDs);
- drafts the response summary;
- contests or accepts the dispute;
- sends dispute alerts by in-app message, Web Push, email and WhatsApp.

Built for the Razorpay x ISB Build Challenge, Track 1. Prototype on Razorpay **test mode** only.

**Hosted:** https://proof-bridge-api.onrender.com · [interactive API docs](https://proof-bridge-api.onrender.com/docs) · [health](https://proof-bridge-api.onrender.com/health)

**Step-by-step demo guide** (payment → mock dispute → proof → submit): [END_TO_END_GUIDE.md](https://github.com/insomniacsuperhero/razorpay-frontend/blob/main/docs/END_TO_END_GUIDE.md)

## How it connects to the other two services

| Service | Repo | Hosted | How the API talks to it |
| --- | --- | --- | --- |
| **Frontend** (React PWA) | [razorpay-frontend](https://github.com/insomniacsuperhero/razorpay-frontend) | https://razorpay-frontend-ashy.vercel.app | The frontend calls this API at `/api/v1` with a Bearer session token. CORS allows only `FRONTEND_ORIGIN` plus `EXTRA_CORS_ORIGINS`. Alert links point at `FRONTEND_ORIGIN/d/<dispute_id>?t=<token>`. |
| **Mock Disputes API** | [razorpay-mock-disputes](https://github.com/insomniacsuperhero/razorpay-mock-disputes) | https://razorpay-mock-disputes.vercel.app | This API calls the mock (`MOCK_DISPUTES_BASE_URL`) with Basic auth using the Razorpay test key and secret: list, fetch, contest, accept, create. The mock calls back `POST /api/v1/webhooks/disputes` with HMAC-signed events, verified with `MOCK_DISPUTES_WEBHOOK_SECRET`. |
| **Razorpay test API** (real) | n/a | https://api.razorpay.com/v1 | Payments, Orders, Payment Links, Documents (`purpose=dispute_evidence`), payment notes. Razorpay calls `POST /api/v1/webhooks/razorpay` for `payment_link.paid` and `payment.captured`, verified with `RAZORPAY_WEBHOOK_SECRET`. |

```
Frontend ──HTTPS + Bearer──► API ──Basic auth──► Mock Disputes API ──signed webhooks──► API
                              │
                              ├──► Razorpay test API (payments, Documents, notes) ◄── Razorpay webhooks
                              ├──► Gemini (reads each file) · Cloudflare R2 (files) · Supabase Postgres (state)
                              └──► Alerts: Web Push (VAPID) · email (Resend) · WhatsApp (Meta Cloud API)
```

Going live: set `DISPUTES_PROVIDER=razorpay` so dispute calls go to `api.razorpay.com`. The mock's create call is the only thing that falls away.

## Tech stack

| Area | Technology | Notes |
| --- | --- | --- |
| Web framework | FastAPI, Uvicorn, Pydantic v2, pydantic-settings | One error shape everywhere: `{"error": {"code", "message"}}` (`app/errors.py`) |
| Database | SQLAlchemy 2, Alembic, Postgres on Supabase (psycopg 3) | SQLite for local dev. Row-level security on (migration 0002); the API connects as the owner |
| File storage | Cloudflare R2 via boto3 (S3 API), presigned PUT/GET | Private bucket. The browser uploads straight to R2 and never sees credentials. `STORAGE_PROVIDER=local` for dev |
| AI | Google Gemini via `google-genai`, structured JSON output | Main Flash model plus an optional fallback model; temperature 0. `AI_PROVIDER=fake` for dev and tests |
| File handling | PyMuPDF (PDF text + page renders), Pillow (images), python-docx + ReportLab (Word → PDF) | Type detected from magic bytes, never the filename |
| Auth | python-jose (HS256 JWT), single-use link tokens, HMAC webhook verification, idempotency keys | |
| Alerts | pywebpush (VAPID Web Push), httpx to Resend (email) and the Meta WhatsApp Cloud API | |
| Hosting | Render (free web service, `render.yaml` blueprint) | Sleeps after 15 minutes idle |
| Tests | pytest, in-memory fakes for Razorpay, the mock, storage and AI | 49 tests |

## Code layout

```
app/
  main.py               FastAPI app, CORS, error handlers, /health
  config.py             every setting; strict checks when APP_ENV=production
  api/v1/routes.py      all routes (sessions, disputes, uploads, evidence, coverage, contest, merchant tabs,
                        notifications, webhooks, demo)
  clients/              razorpay.py · disputes.py (mock | razorpay provider) · storage.py (r2 | local)
                        ai.py (gemini | fake) · messaging.py (push, email, WhatsApp)
  domain/               deterministic rules, no AI: evidence_rules.py · matcher.py · coverage.py · summary.py
  services/             files.py (detect + normalise) · evidence.py (pipeline + cards) · disputes.py · uploads.py
                        webhooks.py · notify.py · merchant.py (app tabs) · context.py
  security/             tokens.py (link → session, admin secret) · hmac_verify.py · idempotency.py
  db/                   models.py · session.py
migrations/versions/    0001 schema · 0002 RLS · 0003 notifications · 0004 push subscriptions
sql/schema.sql          the same schema as plain SQL for the Supabase SQL editor
scripts/                check_integrations.py (read-only contract checks) · vapid_keys.py (Web Push keys)
tests/                  test_domain.py · test_flow.py · test_notify.py
```

## Spin it up locally (no accounts needed)

Python 3.12 (3.9, the macOS default, is too old).

```bash
git clone https://github.com/insomniacsuperhero/razorpay-api-py.git && cd razorpay-api-py
python3.12 -m venv .venv && source .venv/bin/activate      # or: uv venv -p 3.12 && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # defaults: SQLite, local file storage, fake AI, mock disputes
alembic upgrade head            # or set AUTO_CREATE_TABLES=true for a throwaway dev DB
pytest -q                       # 49 passed
uvicorn app.main:app --reload --port 8000
open http://localhost:8000/docs
```

With the defaults, you get the whole flow without Gemini, R2 or Supabase.
- **Local storage:** files are written to `./.uploads` and uploaded through the API itself.
- **Fake AI:** a deterministic extractor that reads text and filename keywords. It is clearly labelled and meant only for development, tests and as a demo fallback.

To connect the local API to the other services:

1. **Frontend:** run it on `http://localhost:5173` (the default `FRONTEND_ORIGIN`) with `VITE_API_BASE_URL=http://localhost:8000/api/v1`.
2. **Mock disputes:** keep `MOCK_DISPUTES_BASE_URL=https://razorpay-mock-disputes.vercel.app`, or run the mock locally and use `http://localhost:4010`. For webhooks to reach a local API, expose it with a tunnel (for example `cloudflared tunnel --url http://localhost:8000`) and set the mock's `WEBHOOK_URL` to `<tunnel>/api/v1/webhooks/disputes`.
3. **Razorpay:** add your `rzp_test_` key and secret to `.env`. Check them without changing anything:
   `python scripts/check_integrations.py pay_XXXXXXXX` (read-only and test-only; it never submits).

## Configuration

All settings live in `.env` locally and in Render's environment in production. `.env.example` lists them all. **Never commit `.env`.**

| Group | Variables | Notes |
| --- | --- | --- |
| App | `APP_ENV`, `FRONTEND_ORIGIN`, `EXTRA_CORS_ORIGINS`, `API_BASE_URL`, `JWT_SIGNING_SECRET`, `DEMO_ADMIN_SECRET` | In production the secrets must be 32+ random bytes; startup fails otherwise |
| Sessions | `ACCESS_TOKEN_MINUTES` (120), `LINK_TOKEN_MINUTES` (30), `ALERT_LINK_MINUTES` (1440) | Link tokens are single use |
| Database | `DATABASE_URL`, `AUTO_CREATE_TABLES` | Supabase → Connect → Session pooler. Prefix with `postgresql+psycopg://` and URL-encode the password |
| Storage | `STORAGE_PROVIDER` (local / r2), `R2_*`, `MAX_UPLOAD_BYTES` (100 MB), `PRESIGN_EXPIRY_SECONDS` (900) | |
| Razorpay | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Only `rzp_test_` keys are accepted |
| Disputes | `DISPUTES_PROVIDER` (mock / razorpay), `MOCK_DISPUTES_BASE_URL`, `MOCK_DISPUTES_WEBHOOK_SECRET` | The webhook secret must equal the mock's `WEBHOOK_SECRET` |
| AI | `AI_PROVIDER` (gemini / fake), `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | Use the current Flash model in your account; the fallback is used on 404, 429 or 5xx |
| App shell | `MERCHANT_ID`, `MERCHANT_NAME`, `DEMO_OPEN_ACCESS` | `DEMO_OPEN_ACCESS=true` lets anyone with the app URL open this **test** merchant without a link. Demo only |
| Alerts | `NOTIFY_CHANNELS`, `REMINDER_HOURS` (24,6), `MERCHANT_EMAIL`, `RESEND_API_KEY`, `NOTIFY_EMAIL_FROM`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `MERCHANT_WHATSAPP`, `WHATSAPP_TEMPLATE`, `WHATSAPP_TEMPLATE_LANG`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` | A channel without its keys is recorded as `skipped`, never an error. Generate VAPID keys once: `python scripts/vapid_keys.py` |

## Deploy to Render

1. **Supabase:** create a project. Run `alembic upgrade head` against the Session-pooler URL, or paste `sql/schema.sql` and then the later migrations into the SQL editor.
2. **Cloudflare R2:** create a private bucket `proof-bridge-evidence` and an Object Read & Write token. Add this CORS rule:
   `[{"AllowedOrigins":["http://localhost:5173","https://razorpay-frontend-ashy.vercel.app"],"AllowedMethods":["PUT","GET","HEAD"],"AllowedHeaders":["Content-Type"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3600}]`
3. **Gemini:** create an API key in Google AI Studio. Use only synthetic or anonymised evidence on the free tier.
4. **Render:** New → Blueprint → this repo. `render.yaml` sets the build, the start command (`alembic upgrade head && uvicorn …`), the `/health` check and generated secrets. Fill in every variable marked `sync: false`.
5. **Webhooks:**
   - Razorpay test dashboard → Settings → Webhooks: add `https://proof-bridge-api.onrender.com/api/v1/webhooks/razorpay` (events `payment_link.paid`, `payment.captured`), with the same secret as `RAZORPAY_WEBHOOK_SECRET`.
   - The mock's Vercel environment: set `WEBHOOK_URL=https://proof-bridge-api.onrender.com/api/v1/webhooks/disputes` and `WEBHOOK_SECRET` to the value of `MOCK_DISPUTES_WEBHOOK_SECRET`, then redeploy the mock.
6. **Frontend:** set `FRONTEND_ORIGIN=https://razorpay-frontend-ashy.vercel.app` so CORS and alert links point at the app.
7. **Reminders (optional):** call `POST /api/v1/notifications/sweep` with `X-Demo-Secret` every 15–30 minutes from any scheduler.

Render's free tier sleeps after 15 minutes. Open `/health` a minute before a demo.

## API overview

All routes are under `/api/v1`; [/docs](https://proof-bridge-api.onrender.com/docs) has every request and response.

| Area | Routes |
| --- | --- |
| Sessions | `POST /sessions/exchange` (alert link token → access token) · `POST /sessions/open` (only with `DEMO_OPEN_ACCESS`) |
| Dispute flow | `GET /disputes/{id}` · `POST /disputes/{id}/timer/start` · `POST /disputes/{id}/uploads` → `PUT <put_url>` → `POST /disputes/{id}/uploads/{upload_id}/complete` · `GET/PATCH/DELETE /disputes/{id}/evidence/{evidence_id}` · `POST …/evidence/{evidence_id}/process` (retry) · `GET /disputes/{id}/coverage` · `PATCH /disputes/{id}/draft` · `POST /disputes/{id}/contest` · `POST /disputes/{id}/accept` |
| App tabs | `GET /merchant/overview`, `/payments`, `/payments/{id}`, `/settlements`, `/products`, `/disputes`, `/notifications`, `/profile` · `POST /merchant/notifications/read[?dispute_id=]` · `/merchant/push/*` |
| Webhooks | `POST /webhooks/razorpay` · `POST /webhooks/disputes` (both HMAC-verified and recorded once per event ID) |
| Admin / demo (`X-Demo-Secret`) | `POST /demo/seed` · `/demo/deep-link/{id}` · `/demo/disputes/{id}/nudge` · `/demo/disputes/{id}/action-required` · `/demo/disputes/{id}/resolve` · `/demo/reset/{id}` · `GET /demo/metrics/{id}` · `POST /notifications/sweep` |

`contest` and `accept` need a fresh `Idempotency-Key` header, so a retried tap never double-submits.

## How proof is processed (the pipeline)

For each file, `services/evidence.py` runs these stages. The app shows them as Upload → Read → Check → Save.

1. **Detect and normalise:**
   - The file type comes from its magic bytes.
   - Images are re-encoded: 1600 px for the AI, 2400 px for Razorpay.
   - PDFs: text is extracted locally, and the 4 most relevant pages are rendered for the AI.
   - Word files are converted to PDF.
2. **Extract (AI):** Gemini returns one validated JSON object: document type, suggested evidence field, readability, quality issues and visible facts.
3. **Validate (rules):** `domain/matcher.py` checks the facts against the real payment, and `domain/evidence_rules.py` decides whether the field is needed for this dispute reason. Together they set the label: Strong, Relevant, Needs attention or Not useful.
4. **Save:** useful files go to Razorpay Documents, which returns a `doc_` ID. Nothing is sent to the bank until the seller submits.

A card that stops updating for 150 seconds is marked as interrupted, so it can never block coverage or submit. The [end-to-end guide](https://github.com/insomniacsuperhero/razorpay-frontend/blob/main/docs/END_TO_END_GUIDE.md#ai-guardrails-and-rules) explains the AI guardrails and every rule.

## Data

Tables: `dispute_runs` (one per dispute: timer, draft summary, submitted time), `evidence_items` (one per file: facts, verdict, `doc_` ID), `upload_sessions`, `merchant_edits` (fact corrections), `link_tokens`, `idempotency_keys`, `webhook_events`, `metric_events`, `notifications`, `push_subscriptions`. The dispute record itself lives in the dispute provider (the mock, or Razorpay).

To start clean before a demo, empty those tables in Supabase (keep `alembic_version`) and reset the mock (see its README).

## Security

- Every secret stays on the server. The browser only sees presigned URLs and short-lived tokens.
- Link tokens are single use and exchanged for a merchant-scoped JWT.
- Webhooks are HMAC-verified and deduplicated by event ID.
- Demo routes need `X-Demo-Secret`.
- Only `rzp_test_` keys are accepted, and production settings are validated at startup.
- Rotate any key that has been pasted into chat or a shared document.
