# Project status

Updated November 22, 2026. The first local URL-to-analysis flow works. Broader review collection and recommendation quality still need validation.

## Repositories

- [Backend](https://github.com/VikasX/product-decision) uses FastAPI, async MongoDB for database health, and SQLite for analysis caching and usage tracking.
- [UI](https://github.com/VikasX/product-decision-ui) uses Next.js, React, TypeScript and Tailwind CSS.

## Completed backend work

- `POST /api/v1/analysis` accepts an Amazon.in product URL, validates its host and ASIN, and removes tracking parameters.
- Collects accessible listing, recent and critical review pages. An isolated headless Chrome session provides a fallback when direct requests collect no reviews.
- Extracts review IDs, text, stars, dates, variants and source links, with bounded collection and duplicate removal.
- Makes one OpenAI request with GPT-5 nano and minimal reasoning. Validates the returned structure and that cited review IDs exist.
- Returns buy, skip or insufficient evidence, reasons, review excerpts, coverage limitations and usage information.
- SQLite caches verified judgments for 24 hours, including uncertain judgments. Cache keys include marketplace, ASIN, model and prompt version.
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

- Backend has 25 passing tests plus Ruff lint and formatting checks. Tests cover persistent caching, budget enforcement before network calls, concurrent duplicate requests, atomic reservations, invalid citations and outputs, credential failure handling and midnight India budget reset.
- UI lint and production build passed.
- A live run for ASIN `B0D5Q7RWF6` collected and analysed eight real reviews, returning an uncertain judgment with source references.
- Repeating the request through the UI returned the saved judgment with no new AI call. The ledger remained at one paid attempt with $0.0014562 reserved, a conservative estimate rather than actual billing.
- Amazon sign-in was verified separately in the browser. Its authenticated session is separate from the backend collector.

## Remaining work

- Establish a reliable signed-in collection flow for the backend. Recent and critical pages can require sign-in or return access challenges.
- Evaluate recommendations against manually reviewed products. Reference validation confirms that IDs exist, but does not prove every generated claim accurately interprets the source review.
- Improve variant handling and balanced review sampling. Current accessible samples can be small and biased.
- Add the bounded agent step that requests more evidence to resolve a specific uncertainty. The current implementation follows a fixed collection and analysis flow.
- Plan deployment, user authentication and operational controls before public hosting. The app is currently a local prototype.
- Review GPT-5 nano availability and pricing before changing the model. It is deprecated but was available during the live test. Cost limits use the documented prices and cover this app's tracked requests.

## Maintaining this record

Update this file when a feature is completed or a limitation changes. Record validation performed and keep backend setup in `backend/README.md` and UI setup in its own README.
