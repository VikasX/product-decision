# AI Product Review Intelligence Agent

FastAPI with async PyMongo and MongoDB. Requires Python 3.12+ with pip and venv, plus a local MongoDB server or MongoDB Atlas cluster.

## Setup (PowerShell)

From the repository root:

```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Skip copying the example if you already configured your local .env.

Set MONGODB_URI in .env to your MongoDB connection string and MONGODB_DATABASE to product_reviews. Credentials belong in this ignored file. Environment variables override .env.

For Atlas, choose Connect > Drivers > Python > PyMongo (Asynchronous). Copy the URI and replace the password placeholder with your database user's URL-encoded password. Allow your current public IP under Database & Network Access, and give the database user readWrite access to product_reviews. Wait for the cluster to finish provisioning.

MONGODB_TIMEOUT_MS defaults to 5000 and bounds database operations. CORS_ORIGINS is a JSON array of exact origins such as ["http://localhost:3000"], defaulting to no cross-origin access. LOG_LEVEL defaults to INFO.

## Connection test and server

Run from backend:

```powershell
.\.venv\Scripts\python -m app.db.check
.\.venv\Scripts\python -m uvicorn app.main:create_app --factory --reload
```

The connection command pings MongoDB, returns exit code 0 on success and 1 on failure, and does not print credentials. Check the URI, database credentials, Atlas IP access, DNS and cluster status if it fails.

Open [Swagger](http://127.0.0.1:8000/docs). GET /api/v1/health reports application liveness. GET /api/v1/health/db pings MongoDB and returns 503 when unavailable. The application creates one client per lifespan and closes it at shutdown. Future routes can use get_database from app.db.session as a FastAPI dependency.

No collections are created by these checks. MongoDB creates the database and collections on the first write. There are currently no data migrations. Add explicit index setup and versioned data migration scripts when application data requires them.

## Developer checks

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
```

Unit tests mock database responses and run without Atlas. Use app.db.check to verify the actual deployment.

## Product analysis

POST /api/v1/analysis accepts an Amazon India product URL as JSON, for example
`{"url":"https://www.amazon.in/dp/B0D5Q7RWF6"}`. The backend collects accessible
listing, recent and critical review pages with bounded requests. When direct requests
cannot collect written reviews, it tries an isolated headless Google Chrome session
through Playwright. Google Chrome must be installed for this fallback. CAPTCHA and
sign-in restrictions may still prevent collection.

Set OPENAI_API_KEY in the ignored backend .env. OPENAI_MODEL defaults to gpt-5-nano,
using minimal reasoning and at most 2,000 completion tokens including reasoning.
Only gpt-5-nano and its 2025-08-07 snapshot have verified pricing in the allowlist.
The model is deprecated but currently available. There are no automatic retries or
fallbacks to another model. At least three written reviews are required for a call.
The result includes source excerpts and validated review references.

## Usage protection

The default application limits are $0.02 per day, $0.10 lifetime and 20 paid attempts
per day. The day resets at midnight Asia/Calcutta. Limits can be changed through
AI_DAILY_BUDGET_USD, AI_LIFETIME_BUDGET_USD and AI_MAX_PAID_ATTEMPTS_PER_DAY. Setting
any budget to zero disables new paid calls while cached results remain available.
OPENAI_MAX_OUTPUT_TOKENS and OPENAI_MAX_INPUT_TOKENS bound each request.

Before any OpenAI request, the app reserves a conservative cost using serialized
UTF-8 request bytes plus 8,192 tokens for protocol framing, the full output cap,
and uncached prices of $0.05 per million input tokens and $0.40 per million output
tokens. Oversized inputs and requests exceeding a budget are rejected before the
network call. Reservations count even after timeout, invalid output or API rejection.
The usage object reports reservations, which may exceed actual billed usage.

SQLite stores the ledger, cache and worker leases at backend/.data/analysis.sqlite3,
or AI_STATE_PATH when set. Keep this file to preserve the lifetime cap across restarts.
Deleting it resets local tracking. These limits cover this application only, not
other consumers of the API key, and rely on the documented model prices.

Verified analyses, including uncertain judgments, cache for 24 hours per marketplace,
ASIN, model and prompt version. Failures cache for one minute and are keyed to a hash
of the credential so updating a rejected key can recover immediately. A 401 also
blocks calls with that credential for five minutes across products. Raw API keys are
never stored in SQLite. Transactions reserve budgets atomically across workers.
A 240-second lease deduplicates concurrent product analyses. After a worker crash,
its paid reservation remains counted and the lease eventually expires.

This initial flow stores reviews locally in SQLite as part of cached results. MongoDB
health checks and configuration continue to work independently.

Sources checked November 22, 2026

- [GPT-5 nano model and pricing](https://developers.openai.com/api/docs/models/gpt-5-nano)
- [Chat Completions API and completion-token limits](https://developers.openai.com/api/reference/resources/chat/methods/create)
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
