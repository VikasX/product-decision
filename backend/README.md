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

## Product-page evidence (current flow)

Only the main /dp/ASIN page is requested. No star-filtered, recent-review or
pagination route is fetched, and no browser fallback runs. Existing bounded
HTTP transport retries remain (one transient retry, no retry for sign-in/CAPTCHA).
The collection deadline is 30 seconds, per-request timeout defaults to 10 seconds.
AMAZON_MAX_REVIEWS defaults to 8 and is capped at 8 even with older larger overrides;
AMAZON_MAX_PAGES is retained for config compatibility but unused by this collector.

Product metadata adds optional customers_say and feedback_topics (sentiment, total/
positive/negative mentions and source text when explicitly present). Missing fields
stay null/empty. Recognized product-summary/insights markup is required; this release
uses stored synthetic fixtures and does not establish coverage of every live layout.
The parser prioritizes negative/mixed topics and low-star written reviews, deduplicates
and retains dates. Only listing-level percentages describe overall ratings.

Both HTTP and LangChain use the same compact JSON: short title, rating metadata,
Amazon AI summary/topics, and at most 8 reviews with IDs, stars, dates and up to
1,000 text characters each. ANALYSIS_EVIDENCE_MAX_BYTES defaults to 10000 (UTF-8),
in addition to existing conservative request bounds. If needed, the service fits a
smaller sample retaining negative evidence and positive comparison. No HTML, image,
price/spec metadata or source URLs are sent to the model. No additional model call
or provider is introduced. Source text may be truncated by extraction/input limits.

A summary of at least 40 characters plus all five rating buckets permits a guarded
analysis with zero/one/two reviews. Without that combination, three written reviews
are required; ratings alone do not trigger spending. Amazon findings have their own
source, exact source quote and optional topic name; they never invent review IDs.
Existing review findings still require valid IDs/exact body quotes and recurring
issues require two distinct reviews. Topic counts are never treated as failure rates.

The HTTP/SDK transports, nano model, output limit, spending ledger and separate
workflow cache namespaces are preserved. The shared version is review-v5-product-page,
so previous 7+7+7 judgments cannot be reused. Response additions have defaults for
legacy cache readability; new structured output includes amazon_findings. UI changes
stay inside the existing product/insight cards, with Amazon AI evidence explicitly
labelled. Export includes summary, topics and source findings.

### One controlled live test

After deployment and with Atlas access available, submit one Amazon.in product
URL once. Check the listing rating metadata, collected/analyzed counts, data-access
limitations and excerpts behind each finding. Expect a More Evidence Needed result
if Amazon restricts access. Do not repeatedly retry a failed paid judgment. A
verified result is cached for 24 hours. No live test was run for this refinement.

## Separate LangChain Lab (local only)

The original `/api/v1/analysis` HTTPX adapter is unchanged. The independent
`POST /api/v1/analysis/langchain` endpoint uses ChatPromptTemplate → ChatOpenAI
→ native Pydantic `Judgment` output → the same evidence validation and response
assembly. GPT-5 nano, minimal reasoning, completion cap and single-call policy
remain in effect. There are no retry, repair, fallback or extra fetching loops.

Dependencies are pinned in pyproject.toml: langchain-openai 1.8.5, langchain-core
1.8.4 and OpenAI SDK 2.45.0. Install with the existing `pip install -e ".[dev]"`
setup command. The SDK pin keeps the tested HTTPX client/error interface stable.
The adapter disables remote tracing and LangChain's global model cache.

To enable local learning/comparison:

1. Set `LANGCHAIN_LAB_ENABLED=true` in the ignored backend `.env`.
2. Start FastAPI bound to `127.0.0.1` (the existing uvicorn command defaults to it).
3. In the UI `.env.local`, set `NEXT_PUBLIC_DEV_MODE=true` and point
   `NEXT_PUBLIC_API_BASE_URL` at `http://127.0.0.1:8000`; restart Next.
4. Open LangChain Lab below the normal application. Running either analysis can
   spend API credit; comparing both can require two separately guarded calls.

The backend switch defaults false, returning 404. When enabled, requests must come
from loopback (`127.0.0.1` or `::1`); other clients return 403. Vercel is always denied,
independently of the UI flag. This release intentionally does not expose a public
experimental endpoint or add an authentication system. Do not expose the enabled
local backend through a loopback reverse proxy/tunnel: that would collapse the
network boundary and needs separately designed authentication. The public frontend
flag controls visibility only and grants no backend access.

LangChain keys append `:langchain-v1` to the existing versioned result/failure/lease
key, so HTTP judgments are never returned as LangChain judgments. Both workflows
share the exact same usage store, MongoDB ledger, daily/lifetime caps and credential
failure lockout. LangChain first reuses reviews/metadata from a still-valid HTTP
cache entry, while producing its own judgment; otherwise it runs the existing
bounded product-page collector once. It caches its own result for the existing 24-hour TTL.
An additional conservative serialization allowance is reserved before the call.

Mocked transport tests exercise real LangChain/SDK serialization, structured parsing,
provider errors, no retries, source validation, shared limits and distinct caches.
No live Amazon/OpenAI calls were made for this addition. New native schema behavior
against a live model is unverified; failures retain the reservation without retrying.
The Python 3.14 test environment reports upstream LangSmith deprecation warnings.

Reference: [LangChain Pydantic structured output](https://reference.langchain.com/python/langchain-openai/chat_models/base/ChatOpenAI/with_structured_output).

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

Local SQLite stores the ledger, cache and worker leases at backend/.data/analysis.sqlite3,
or AI_STATE_PATH when set. Keep this file to preserve the lifetime cap across restarts.
Deleting it resets local tracking. These limits cover this application only, not
other consumers of the API key, and rely on the documented model prices.

Verified analyses, including uncertain judgments, cache for 24 hours per marketplace,
ASIN, model and prompt version. Failures cache for one minute and are keyed to a hash
of the credential so updating a rejected key can recover immediately. A 401 also
blocks calls with that credential for five minutes across products. Raw API keys are
never stored in either state backend. Atomic reservations enforce budgets across workers.
A 240-second lease deduplicates concurrent product analyses. After a worker crash,
its paid reservation remains counted and the lease eventually expires.

Local development stores cached review excerpts in SQLite. On Vercel the app automatically
uses shared MongoDB collections for the ledger, results, leases and credential-failure cache.
It makes conditional single-document reservations without requiring multi-document transactions.
All Vercel instances share the same daily and lifetime caps when using the same database.
Local SQLite and deployed MongoDB maintain separate spending totals. Deleting the shared
ledger or changing databases resets deployed tracking. Storage outages return HTTP 503
before a new OpenAI request can be made.

## Vercel deployment

Set the Vercel project root to `backend`. The exported `app.main:app` ASGI application
is declared in pyproject.toml. Importing it does not initialise storage or require a local
.env. Runtime clients and required configuration initialise during application startup.
The function allows 300 seconds, and local environments, state files and tests are excluded
from the deployment bundle through vercel.json.
The function runs in Mumbai (`bom1`), near the Atlas cluster and Amazon India.

Set these exact environment variable names in Vercel's project settings

- MONGODB_URI containing the Atlas connection URI, including its database credentials
- MONGODB_DATABASE containing the database the Atlas user can read and write
- OPENAI_API_KEY containing the OpenAI API key

The automatically provided VERCEL=1 selects MongoDB state, including when an old
AI_STATE_BACKEND=sqlite override exists. AI_STATE_BACKEND=mongodb can also enable it
locally for verification. Required collections are ai_usage_ledger, ai_analysis_state
and ai_auth_failures. Atlas network access must allow the deployment's outbound access.

The deployed frontend https://product-decision-ui.vercel.app is allowed by default.
Additional origins can be supplied in CORS_ORIGINS as a JSON array. Configure the UI's
NEXT_PUBLIC_API_BASE_URL to https://product-decision.vercel.app and rebuild the UI.
GET /api/v1/health checks startup and GET /api/v1/health/db checks Atlas access.

Chrome fallback is available locally. Vercel uses HTTP collection only and reports unavailable
reviews when Amazon returns a challenge or requires sign-in. A reliable deployed review
source remains to be validated. Public access also means visitors share this application's
configured spending caps.

[Vercel FastAPI deployment reference](https://vercel.com/docs/frameworks/backend/fastapi)

Sources checked November 22, 2026

- [GPT-5 nano model and pricing](https://developers.openai.com/api/docs/models/gpt-5-nano)
- [Chat Completions API and completion-token limits](https://developers.openai.com/api/reference/resources/chat/methods/create)
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
