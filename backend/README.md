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

## Phase 1 refinement

Collection reads the listing first, then recent-sorted 1-star, 2-star, 3-star,
4-star and 5-star pages and unfiltered recent reviews. Observed pagination links
are followed only within the same Amazon.in ASIN and the page budget. Review IDs
and normalized body text deduplicate repeated reviews. Variant and raw date text
are retained, with ISO dates added only when the format can be parsed reliably.

When present in product-scoped markup, the parser reads the overall rating, total
rating count and displayed star percentages. Missing values stay unavailable.
Rating percentages describe Amazon's listing, while sample counts describe only
collected written reviews. Counts of ratings are not counts of written reviews.

The sample contains up to 7 negative (1–2★), 7 neutral (3★) and 7 positive (4–5★)
reviews, at most 21 total, newest reliably dated accessible text first per category.
Missing categories are not filled with extra reviews from another category. Reviews
without integer star ratings are excluded from the balanced sample. This is an
investigation sample, not a representative satisfaction poll. Unknown dates stay
unknown. The listing is still read for metadata and fallback review text.

AMAZON_MAX_REVIEWS defaults to 21 (hard ceiling 21; existing larger overrides remain
readable but cannot enlarge the sample) and AMAZON_MAX_PAGES defaults to 7. Smaller
page budgets may leave routes unvisited; limitations report that. Each request has a
10-second timeout, with at most one retry for transport failures or HTTP
500/502/503/504. The overall collection deadline is 90 seconds. Sign-in redirects,
CAPTCHAs and access-denied responses are not retried. Once Amazon restricts the
review route, sibling filters are skipped and reported as unavailable. Body text
is bounded to 4,000 characters per review. Local Chrome fallback remains bounded;
Vercel continues using HTTP collection.

The HTTP client maintains cookies only within one collection request. Local Chrome
uses a fresh isolated context. Neither reuses a user's signed-in browser session or
stored Amazon credentials. An incognito-accessible page is not a guarantee of access
from Vercel. No CAPTCHA solving, stealth, proxies or login workflow is introduced.

Listing-scoped image URL, observed price text and product-overview specifications
are optional. Unavailable values remain null/empty. No discounts or warranty are
inferred. Images must use known Amazon image hosts over HTTPS. The analysis adds
source-verified reliability_findings and a short key_takeaway in the same single nano
request. Neutral experiences, variants, recency and contradictions are explicitly
included in the interpretation instructions. These fields are additive in responses;
old cached records remain readable. New collection/analysis keys use
review-v4-balanced-777 without resetting any usage ledger or spending caps.

Previous live runs established that recent/filtered pages can redirect to sign-in,
return HTTP errors or present CAPTCHAs. These are source-access limitations. This
refinement was tested with stored synthetic HTML and mocked responses, not fresh
Amazon requests, so greater live coverage has not been established.

Structured judgments now use worth_considering, proceed_with_caution,
not_recommended or insufficient_evidence. Positive and negative findings include
matching source excerpts. Recurring issues require at least two distinct supporting
reviews and distinguish defects, missing features, usability, delivery, preferences
and other concerns. Python calculates dates, star counts and supporting-review
counts. Quotes and IDs are checked against the selected evidence before a result
can be cached. These checks establish traceability, not the correctness of every
interpretation.

If collected evidence exceeds the input limit, Python selects a bounded subset
balancing available negative, neutral and positive evidence. The response
keeps all collected reviews and reports the number actually analysed. GPT-5 nano,
the 2,000 completion-token default, one-call policy and spending caps remain in use.
The prompt version is now review-v4-balanced-777, so a product's first judgment needs a
new guarded call. Old cache records remain readable and the budget ledger persists.

The response adds product rating metadata, review titles and ISO dates,
positive_findings, negative_findings, recurring_issues, reliability_findings,
key_takeaway and sample_statistics, plus optional listing image/price/specifications.
The existing reasons field is retained for compatibility.

### One controlled live test

After deployment and with Atlas access available, submit one Amazon.in product
URL once. Check the listing rating metadata, collected/analyzed counts, data-access
limitations and excerpts behind each finding. Expect a More Evidence Needed result
if Amazon restricts access. Do not repeatedly retry a failed paid judgment. A
verified result is cached for 24 hours. No live test was run for this refinement.

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
