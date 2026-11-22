# BuyWise AI — project status

Updated November 22, 2026. Backend and UI live in separate repositories, both on main.

## Focused demo corrections (local, not deployed)

- Removed the repeated selected-product panel and empty pre-analysis dashboard.
  Gesto uses one compact selectable card; source summary/topics/snippets stay inside
  the collapsed View Source Evidence section. One existing premium report renders
  after Analyze; its raw source block is hidden only for Demo Mode.
- Local and shared MongoDB records showed technical failures with zero full reviews,
  but did not retain the provider response or exception. Their exact historical
  rejection cannot be reconstructed. No additional paid request was made.
- Fixed a reproducible evidence-validation mismatch: a mixed topic can support a
  specifically negative/positive quoted finding. Explicit single-sentiment topics,
  exact source quotes and review citation checks remain enforced on both sides.
- Made the zero-review prompt explicit. HTTP parsing now distinguishes truncation,
  refusals, envelope/schema errors and evidence failures, logging only safe metadata
  and validation field/type information. No raw model/source text or credentials log.
- Demo technical failures return HTTP 502; pre-call guard failures return 503.
  Genuine insufficient-evidence judgments remain valid HTTP 200 reports. Cached
  technical failures still prevent a second paid attempt for 60 seconds. Caps,
  transports, dataset and store implementations remain unchanged; prompt version v7
  keeps judgments under the earlier validation rules from masking this correction.
- Verification: 43 targeted backend tests and 28 frontend tests passed, plus TypeScript,
  changed-file lint (existing long comment lines excluded in Python), and frontend
  production build. Browser tested selection, collapsed source evidence, one report,
  light/dark and 390px layout using an explicitly labelled mock provider response.
  The real local backend was restored, with no live model/Amazon analysis performed.

## Saved-product Demo Mode (local implementation)

- Home page now browses the category/product catalog and explicitly analyzes a saved
  dataset. The main demo path never invokes Amazon scraping. Existing URL endpoints,
  HTTPX/SDK integrations, MongoDB/SQLite stores and caps remain available unchanged.
- `backend/product/catalog.json` enables only `led/prd1/dataset.json` (Gesto). All
  nine source screenshots were inspected. Topic counts are explicit, not estimates;
  eight partial topic snippets have no full-review IDs/dates/stars, so reviews stay empty.
- The original product folder moved under backend for Vercel bundling. Headphone
  screenshot files are retained but have no enabled dataset/category in this release.
- Shared cache orchestration now has one default-preserving key hook. Demo keys
  contain ID/version/content hash/model/prompt/workflow; data and live judgments cannot mix.
- New targeted checks: five demo backend tests plus 31 existing adapter/product-page
  tests pass (36 total). Four demo frontend tests plus 23 existing report/adapter tests
  pass (27 total); TypeScript, changed-file lint and production build pass. Browser
  verified real catalog/selection/evidence inspection and both themes, without Analyze.
- Paid native model output and deployed bundling are not live-verified; no paid calls,
  production deployment or spending-limit changes were made for this implementation.

## Current product-page simplification

- FLYNGO investigation: an ordinary HTTP GET returned a 3,793-byte CAPTCHA page,
  while the browser product page exposed 8 reviews, 262 ratings and all 5 histogram
  buckets. The original production HTML was not retained, so its missing sections
  cannot be reconstructed. Server/browser access differs; this is not an AI quota error.
- Added support for the observed newer `overall-summary` / `aspect-list` widget
  and `reviewTitle` hook. Partial/blocked evidence now explicitly states that no
  paid analysis was requested. Cache version: review-v6-product-widget. These
  changes do not circumvent Amazon restrictions or guarantee Vercel review access.

- The active collector requests only the product listing, no filtered/recent/paginated
  review pages and no browser fallback. It retains up to 8 unique accessible written
  reviews and available overall ratings, Customers say text and feedback topics/counts.
- Both nano integrations use the same compact evidence JSON, default 10,000 UTF-8
  bytes, up to 1,000 characters per review. Negative/mixed evidence is prioritized.
  Existing transports, spending caps, separate workflow caches and API fields remain.
- A usable Amazon summary + all five histogram buckets can support analysis with
  fewer than three reviews. Summary/topic findings cite their own source quotes;
  written-review IDs and recurring-review validation remain separate and intact.
- Customers say/topics appear in the existing product card; source findings augment
  existing insight cards. Missing source data stays unavailable and repetitive
  filtered-page warnings are removed. Cache version: review-v5-product-page.
- Validation uses stored synthetic HTML and mocked HTTP/SDK transports only. No new
  live Amazon/OpenAI requests or spending-limit changes. Live markup coverage and
  native model output remain unverified; the underlying listing can still be blocked.

The sections below record earlier releases and are historical where superseded above.

## Current implementation

- Amazon.in URL → canonical ASIN → accessible product/review extraction → one GPT-5 nano call → validated evidence → shared MongoDB cache → Next.js results. Four verdicts: Worth Considering, Proceed with Caution, Not Recommended, More Evidence Needed.
- Collection attempts the listing plus recent-sorted 1, 2, 3, 4 and 5-star routes and recent reviews. It retains up to 7 negative, 7 neutral and 7 positive unique reviews, at most 21. Dates, titles, stars, variants and source references are preserved. Missing categories are not backfilled. Latest accessible dated reviews are prioritised within each group.
- Default limits: 7 page fetches, 10 seconds per request, one transient retry, 90 seconds overall. Observed pagination stays within the same ASIN and page budget. Sign-in, CAPTCHA and access denials stop sibling protected routes. Local Chrome fallback is bounded; Vercel uses HTTP only.
- Product-scoped overall rating, rating count and displayed histogram remain separate from the deliberately balanced written-review sample. Optional observed price, safe Amazon-hosted image and overview specifications are extracted only when available. No price, discount or warranty is invented.
- The single nano response includes positive/negative findings, classified recurring issues, reliability findings and a key takeaway. Quotes and IDs are verified against the supplied review text; recurring issues require two distinct reviews and strong negative verdicts require recurring product evidence. Python computes sample counts and date range. Model interpretation itself still needs human evaluation.
- Cost protections remain: $0.02/day, $0.10 lifetime, 20 paid attempts/day by default, conservative reservations before requests, no model retries/fallbacks, bounded input/output, concurrency leases and 24-hour verified cache. Failures cache briefly. Prompt/collection version is review-v4-balanced-777; existing ledgers are preserved.
- UI implements the approved Stitch light/dark screens with one responsive layout: ivory/forest/gold or midnight/emerald/gold, persistent local theme choice, product + verdict row, three analytics cards, three insight cards, limitations, expandable reviews and a plain-text export using the current response. The only overall percentages come from the Amazon histogram; missing components stay unavailable. The sentiment ring shows actual collected rating counts and identifies the sampling bias. AI-analyzed and collected counts remain distinct.

## Verification for this release

### Independent LangChain workflow

- Added the LangChain adapter/service and `/api/v1/analysis/langchain`. The original
  HTTP adapter and endpoint remain intact; small default-preserving hooks in the
  service reuse collection, sample fitting, validation, caching and result assembly.
- LangChain uses one nano request with ChatPromptTemplate and Pydantic structured
  output. It reuses a valid HTTP cache's review evidence where available, otherwise
  the existing 7+7+7 collector. Its judgments/failures/leases have their own namespace;
  both workflows share the spending ledger and credential lockout. Tracing, SDK
  retries, model fallbacks and automatic repair calls are disabled.
- The collapsible LangChain Lab is below the main UI. NEXT_PUBLIC_DEV_MODE must
  equal true to show it; missing/false hides it. Its own input/request/loading/error
  state and scoped report anchors leave the original form/report independent.
- Independently, LANGCHAIN_LAB_ENABLED defaults false; enabled requests are limited
  to loopback and denied on Vercel. No new public authentication system was added.
  Local setup and the reverse-proxy boundary are documented in backend/README.md.
- Validation: 18 new LangChain tests + 25 relevant HTTP/storage regression tests,
  20 frontend tests, Ruff/ESLint, TypeScript and default-off production build passed.
  Mocked transports exercise real LangChain and the pinned OpenAI SDK. No paid or
  fresh Amazon calls were made. Browser checks cover coexistence of both reports,
  scoped references, isolated errors and the existing themes; native live output
  remains unverified.

The following verification records the preceding BuyWise theme/7+7+7 release:

- Backend: 73 mocked tests, Ruff lint and formatting. Coverage includes category caps/latest dates/deduplication, shortages, neutral star routes, bounded failures, metadata, quotes, structured analysis, API/cache compatibility and existing budget/storage protections.
- UI: 17 tests, ESLint, TypeScript and production build. Covers nested payload validation, source URLs/quotes, histogram aggregates with missing buckets, actual sample counts and report content for all four verdicts.
- Browser verification uses a loopback fixture API, not production analysis: desktop/mobile themes, persisted preference after reload, all verdicts, empty/partial data, malformed response/503 errors, nested evidence links and downloaded report. No horizontal overflow at 390px.
- No Amazon or OpenAI calls were made for this release's validation. The 7+7+7 strategy works against mocked pages; obtaining all 21 live reviews is not guaranteed. New live model output is unverified.

## Source and infrastructure limitations

- Prior hosted requests established sign-in redirects, CAPTCHAs and HTTP failures. A prior Mumbai-hosted UGREEN run collected 13 real reviews and reused the cache; that does not establish coverage for other products or this revised strategy.
- The HTTP collector has an anonymous per-request cookie jar; isolated local Chrome does not inherit the user's authenticated browser. No credentials, session-sharing, anti-bot bypass, paid source or new auth system was added.
- Public users share the configured spending caps. Storage outages fail closed before a new AI request. Local SQLite and deployed MongoDB have separate ledgers.
- Atlas network access was explicitly approved temporarily for 6 hours, expiring November 22 at approximately 18:25 IST. A durable permitted network path still needs to be arranged. This release does not extend the rule or modify network access.
- GPT-5 nano remains the sole model, with the existing verified price allowlist. Review availability/pricing before any model change. No LangChain/LangGraph, app MCP, RAG, agent orchestration, database migration or extra provider was introduced.

## Repositories and operation

- Backend: https://github.com/VikasX/product-decision — deployment https://product-decision.vercel.app.
- UI: https://github.com/VikasX/product-decision-ui — deployment https://product-decision-ui.vercel.app.
- Setup, configuration, budget details and one controlled live-test procedure: [backend guide](backend/README.md).
- For one live test, refresh the hosted UI, submit one product URL once and inspect counts, quotes and limitations. First access under the new cache version can make one budget-guarded paid request when at least three reviews fit. Repeat access should reuse the verified cache. No live test is run automatically.

Maintain this file with each completed change and record actual validation separately from intended or live-unverified behavior.
