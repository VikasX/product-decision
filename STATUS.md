# Project status

Updated November 22, 2026. The local URL-to-analysis flow works and both Vercel deployments are connected. Temporary Atlas access restored database connectivity. Amazon currently challenges hosted review requests with CAPTCHAs, so hosted review collection remains blocked.

## Repositories

- [Backend](https://github.com/VikasX/product-decision) uses FastAPI, MongoDB for deployed analysis state and usage limits, and SQLite for local analysis state.
- [UI](https://github.com/VikasX/product-decision-ui) uses Next.js, React, TypeScript and Tailwind CSS.

## Completed backend work

- `POST /api/v1/analysis` accepts an Amazon.in product URL, validates its host and ASIN, and removes tracking parameters.
- Collects accessible listing, recent and critical review pages. An isolated headless Chrome session provides a fallback when direct requests collect no reviews.
- Extracts review IDs, text, stars, dates, variants and source links, with bounded collection and duplicate removal.
- Makes one OpenAI request with GPT-5 nano and minimal reasoning. Validates the returned structure and that cited review IDs exist.
- Returns buy, skip or insufficient evidence, reasons, review excerpts, coverage limitations and usage information.
- SQLite locally and shared MongoDB on Vercel cache verified judgments for 24 hours, including uncertain judgments. Cache keys include marketplace, ASIN, model and prompt version.
- Atomic reservations enforce default limits of $0.02 per India calendar day, $0.10 total and 20 paid attempts per day. Reservations and cache survive restarts.
- Bounds input and output, deduplicates concurrent requests, retains reservations after failed calls and makes no automatic retry or model fallback.
- Preserves database health routes and adds frontend CORS support. Credentials and local usage/cache files are ignored by Git.

## Completed UI work

- Amazon.in URL form submits to the backend with input validation, loading and error states.
- Shows the verdict, explanation, collected and analysed review counts, and collection limitations.
- Reasons link to expandable source reviews with stars, dates, variants and Amazon links.
- Shows the selected model, cache reuse and conservative cost reservations. Backend budget-limit messages remain visible.
- Retains API health status and prevents overlapping submissions from the form.

## Validation completed

- Backend has 36 passing tests plus Ruff lint and formatting checks. Tests cover persistent caching, budget enforcement before network calls, concurrent duplicate requests, atomic reservations, invalid citations and outputs, credential failure handling and midnight India budget reset. Deployment tests cover import without storage initialisation, Vercel state selection, shared MongoDB conditional budget updates and fail-closed storage errors. Diagnostics tests confirm that database credentials and exception text do not appear in connection-failure logs.
- Real Atlas verification passed against temporary collections inside the configured database, covering concurrent reservations, India day rollover, shared cache, lease ownership and credential-failure sharing. The exact QA collections were removed. No OpenAI requests were made for this deployment verification.
- UI lint and production build passed.
- A live run for ASIN `B0D5Q7RWF6` collected and analysed eight real reviews, returning an uncertain judgment with source references.
- Repeating the request through the UI returned the saved judgment with no new AI call. The ledger remained at one paid attempt with $0.0014562 reserved, a conservative estimate rather than actual billing.
- Amazon sign-in was verified separately in the browser. Its authenticated session is separate from the backend collector.
- Vercel deployed the backend successfully after the ASGI entrypoint fix. Application and database health endpoints returned 200 after an explicitly approved temporary Atlas access change. This access expires November 22, 2026 at approximately 18:25 India time. A durable permitted network path remains necessary.
- The UI Production environment sets NEXT_PUBLIC_API_BASE_URL to https://product-decision.vercel.app. Its Git-triggered rebuild completed and the hosted UI displays API connected. The former storage 503 is resolved. A hosted test for B0D5Q7RWF6 returned insufficient evidence because Amazon challenged collection with CAPTCHAs. Zero reviews were collected and no OpenAI call was made.

## Remaining work

- Establish a reliable signed-in collection flow for the backend. Recent and critical pages can require sign-in or return access challenges.
- Evaluate recommendations against manually reviewed products. Reference validation confirms that IDs exist, but does not prove every generated claim accurately interprets the source review.
- Improve variant handling and balanced review sampling. Current accessible samples can be small and biased.
- Add the bounded agent step that requests more evidence to resolve a specific uncertainty. The current implementation follows a fixed collection and analysis flow.
- Arrange a durable permitted network path from Vercel to Atlas before the temporary access expires. Hosted Amazon requests currently encounter CAPTCHAs and Chrome fallback is local-only, so a reliable deployed review source is needed. User authentication and public-access controls remain pending.
- Review GPT-5 nano availability and pricing before changing the model. It is deprecated but was available during the live test. Cost limits use the documented prices and cover this app's tracked requests.

## Maintaining this record

Update this file when a feature is completed or a limitation changes. Record validation performed and keep backend setup in `backend/README.md` and UI setup in its own README.
