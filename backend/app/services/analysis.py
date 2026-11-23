import asyncio
import logging
import time
import uuid

import httpx

from app.core.config import Settings
from app.integrations.amazon import collect_reviews
from app.integrations.llm import AttemptCharge, ModelOutputError, analyze_reviews, prepare_request
from app.schemas.analysis import AnalysisResponse, Coverage, Product, Reason, RecurringIssue, Review
from app.services.review_evidence import sample_statistics, select_page_reviews, usable_summary
from app.services.usage import (
    PROMPT_VERSION,
    CostProtectionError,
    create_usage_store,
    credential_fingerprint,
)

logger = logging.getLogger(__name__)
TECHNICAL_FAILURE_PREFIX = "AI analysis failed"


# Shared flow: cache -> Amazon -> compact evidence -> one judgment -> report/cache.
class AnalysisService:
    # An empty suffix identifies the normal HTTP workflow; the subclass supplies a different suffix.
    cache_namespace = ""

    def cache_key(self, asin: str) -> str:
        # Keep live workflow keys unchanged; saved datasets override this boundary.
        return f"amazon.in:{asin}:{self.config.openai_model}:{PROMPT_VERSION}{self.cache_namespace}"

    # Python calls this when creating the service. Tests can supply a fake store to avoid real databases.
    def __init__(self, config: Settings, *, store=None):
        # Keep the validated settings available to later collection and model methods.
        self.config = config
        # Use the supplied store or create the configured SQLite/MongoDB implementation.
        self.store = store if store is not None else create_usage_store(config)

    # Canonical URL in, API report out; leases prevent duplicate paid work for this cache key.
    async def analyze(self, url: str) -> AnalysisResponse:
        # The request validator already removed tracking parameters, so the final path piece is the product ID.
        asin = url.rsplit("/", 1)[-1]
        # Combine product, model, evidence version and workflow so unrelated reports never share a cache entry.
        key = self.cache_key(asin)
        # Credential-specific failures do not block recovery after a key is replaced.
        failure_key = f"{key}:failure:{credential_fingerprint(self.config)}"
        # Generate a unique worker ID; it proves that this request owns its temporary processing lease.
        owner = uuid.uuid4().hex
        # Use a monotonic clock for the wait deadline so system clock adjustments do not extend it.
        deadline = time.monotonic() + 210
        # Keep checking for a completed cached report while another request may own the work.
        while time.monotonic() < deadline:
            # Look for either a verified report or a recent failure before collecting data or spending money.
            cached = self.store.cached(key) or self.store.cached(failure_key)
            # A saved report is available, so no new Amazon or OpenAI request is needed.
            if cached is not None:
                # Tell the UI this response came from storage rather than a new model analysis.
                cached.cached = True
                # Refresh the displayed budget totals even though the report itself is reused.
                cached.usage = self.store.usage(cached=True)
                # Finish immediately with the reused report.
                return cached
            # Try to become the single worker allowed to perform this product/workflow analysis.
            if self.store.acquire(key, owner):
                break
            # Pause without busy-looping while another worker finishes; this is not a model retry.
            await asyncio.sleep(0.5)
        else:
            # The wait expired without a lease. Return an honest pending-work message instead of duplicating work.
            return AnalysisResponse(
                status="insufficient_evidence",
                product=Product(asin=asin, title=f"Amazon product {asin}", url=url),
                verdict="insufficient_evidence",
                summary="This product is already being analysed. Please try again shortly.",
                reasons=[],
                reviews=[],
                coverage=Coverage(reviews_analyzed=0, limitations=[]),
                usage=self.store.usage(),
            )
        try:
            # Another worker can finish between the cache read and lease acquisition.
            # Look for either a verified report or a recent failure before collecting data or spending money.
            cached = self.store.cached(key) or self.store.cached(failure_key)
            # A saved report is available, so no new Amazon or OpenAI request is needed.
            if cached is not None:
                # Tell the UI this response came from storage rather than a new model analysis.
                cached.cached = True
                # Refresh the displayed budget totals even though the report itself is reused.
                cached.usage = self.store.usage(cached=True)
                # Finish immediately with the reused report.
                return cached
            # Await evidence collection and optional model analysis; verified controls the cache lifetime.
            result, verified = await self.run_analysis(url, asin)
            # Keep verified judgments for 24 hours; retryable failures expire after 60 seconds.
            self.store.finish(
                key,
                owner,
                key if verified else failure_key,
                result,
                ttl=86400 if verified else 60,
            )
            return result
        # This block runs even if analysis raises an error, so the processing lease is not left behind.
        finally:
            # Release only our own lease; another worker must not lose its lock.
            self.store.release(key, owner)

    # Return the report and whether a model judgment passed validation for long-term caching.
    async def run_analysis(self, url: str, asin: str) -> tuple[AnalysisResponse, bool]:
        # Receive observed product data, source reviews and collection limitations from the collector.
        product, reviews, limitations = await self.collect_evidence(url, asin)
        # Start with an insufficient-evidence report. A validated model judgment can fill it in later.
        result = AnalysisResponse(
            status="insufficient_evidence",
            product=product,
            verdict="insufficient_evidence",
            summary="Could not collect enough product-page evidence to support a buying decision.",
            reasons=[],
            reviews=reviews,
            coverage=Coverage(reviews_analyzed=0, limitations=limitations),
            sample_statistics=sample_statistics(reviews),
        )
        # Track the conservative reservation for this attempt so usage remains visible after failures.
        charge = AttemptCharge()
        # Until validation succeeds, the result must not receive the long-lived verified cache entry.
        verified = False
        # Choose a subset whose compact prompt fits the configured input limits.
        analysis_reviews = self.fit_review_sample(product, reviews)
        # Explain when the model receives fewer reviews than the collector found.
        if len(analysis_reviews) < len(reviews):
            result.coverage.limitations.append(
                f"AI input limits allow {len(analysis_reviews)} of {len(reviews)} "
                "collected reviews "
                "to be analysed. The subset prioritises negative evidence and "
                "positive evidence."
            )
        # Ratings alone do not justify spending; a usable summary plus histogram is an alternative.
        if len(analysis_reviews) >= 3 or usable_summary(product):
            try:
                # The adapter reserves budget, calls the model once and checks its output before returning.
                judgment = await self.judge_reviews(product, analysis_reviews, charge)
                # Copy the validated buying verdict into the public report.
                result.verdict = judgment.verdict
                # A valid response can still honestly conclude that the evidence is insufficient.
                result.status = (
                    "insufficient_evidence"
                    if judgment.verdict == "insufficient_evidence"
                    else "completed"
                )
                # Copy the model explanation only after schema and source checks have succeeded.
                result.summary = judgment.summary
                # Keep supported positive findings separate from concerns and reliability findings.
                result.positive_findings = judgment.positive_findings
                # Attach supported concerns with their review citations.
                result.negative_findings = judgment.negative_findings
                # Attach supported reliability findings without claiming unsupported long-term testing.
                result.reliability_findings = judgment.reliability_findings
                # Store the short practical conclusion used by the UI and export.
                result.key_takeaway = judgment.key_takeaway
                # Preserve Amazon-summary findings as a separate evidence type from direct reviews.
                result.amazon_findings = judgment.amazon_findings
                # Citation counts come from unique review IDs, not model arithmetic.
                result.recurring_issues = [
                    RecurringIssue(**issue.model_dump(), mention_count=len(set(issue.review_ids)))
                    for issue in judgment.recurring_issues
                ]
                # Build the older reasons list from validated findings so earlier UI/report consumers still work.
                result.reasons = [
                    Reason(text=finding.text, review_ids=finding.review_ids)
                    for finding in (
                        judgment.negative_findings
                        + judgment.positive_findings
                        + judgment.recurring_issues
                    )
                ]
                # Count only the reviews actually included in this successful model request.
                result.coverage.reviews_analyzed = len(analysis_reviews)
                # Both model parsing and evidence validation passed, so the result can be cached for 24 hours.
                verified = True
            # A local configuration, size or budget guard prevented a paid model call.
            except CostProtectionError as exc:
                result.summary = str(exc)
                result.coverage.limitations.append("No new AI request was made.")
            # OpenAI HTTP errors are separate from Amazon evidence collection failures.
            except httpx.HTTPStatusError as exc:
                # Log only known diagnostic labels, never the provider's free-form message,
                # request body, review text or Authorization header.
                try:
                    error = exc.response.json().get("error", {})
                    code = error.get("code") or error.get("type")
                    parameter = error.get("param")
                except (ValueError, AttributeError, TypeError):
                    code = parameter = None
                safe_code = code if isinstance(code, str) and code in {
                    "invalid_json_schema", "invalid_request_error", "context_length_exceeded",
                    "unsupported_value", "unsupported_parameter", "model_not_found",
                    "rate_limit_exceeded", "insufficient_quota",
                } else "unknown"
                safe_parameter = parameter if isinstance(parameter, str) and parameter in {
                    "response_format", "response_format.json_schema", "max_completion_tokens",
                    "model", "messages", "reasoning_effort", "temperature",
                } else "unknown"
                logger.warning(
                    "LLM request failed exception=HTTPStatusError http_status=%s provider_code=%s parameter=%s",
                    exc.response.status_code, safe_code, safe_parameter,
                )
                # Translate common provider status codes into safe, understandable troubleshooting messages.
                messages = {
                    401: "OpenAI rejected the API key. Update the key and restart the backend.",
                    403: "OpenAI denied access. Check the API project's model permissions.",
                    429: "OpenAI usage limit reached. Check API credit and usage limits.",
                }
                result.summary = messages.get(
                    exc.response.status_code,
                    "OpenAI analysis is unavailable. Please try again later.",
                )
                result.coverage.limitations.append(
                    f"AI analysis failed with OpenAI HTTP {exc.response.status_code}. "
                    "Collected review excerpts remain available below."
                )
            # Failed/invalid responses may cost tokens; keep the reservation and never retry here.
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                code = (
                    exc.code
                    if isinstance(exc, ModelOutputError)
                    else (
                        "request_timeout"
                        if isinstance(exc, httpx.TimeoutException)
                        else "invalid_response"
                    )
                )
                logger.warning("LLM analysis failed exception=%s code=%s", type(exc).__name__, code)
                result.summary = (
                    "AI analysis is temporarily unavailable; the saved evidence was not rejected."
                )
                result.coverage.limitations.append(f"{TECHNICAL_FAILURE_PREFIX} ({code}).")
                result.coverage.limitations.append(
                    "The attempt is counted against the budget. No automatic retry was made."
                )
        else:
            # Distinguish missing Amazon evidence from a paid model failure.
            if not reviews and not product.customers_say:
                result.summary = (
                    "Amazon review evidence was unavailable to the server. "
                    "No paid AI analysis was requested."
                )
            result.coverage.limitations.append(
                "Analysis needs at least three written reviews or a usable Amazon summary "
                "with the full rating distribution."
            )
        # Read the latest ledger totals and include this attempt reservation in the returned report.
        result.usage = self.store.usage(reserved=charge.reserved)
        # Return both the public report and the internal long-cache eligibility flag.
        return result, verified

    # Shrink input until it fits configured bounds while retaining negative evidence.
    def fit_review_sample(self, product: Product, reviews: list[Review]) -> list[Review]:
        # Summary-backed evidence may use zero reviews; otherwise the minimum remains three.
        minimum = 0 if usable_summary(product) else 3
        # Try smaller subsets locally, starting at eight or fewer. This loop never calls OpenAI.
        for count in range(min(len(reviews), 8), minimum - 1, -1):
            # Use shared negative-prioritizing selection, or an empty sample when the summary alone is usable.
            selected = select_page_reviews(reviews, count) if count else []
            try:
                # Build and size-check the prospective request without transmitting it.
                self.prepare_llm_request(product, selected)
                # The first fitting subset is the largest allowed one; use it for the single model request.
                return selected
            # If this subset is too large, try a smaller one without spending money.
            except CostProtectionError:
                continue
        return []

    # One product-page fetch; the LangChain subclass can reuse cached source evidence.
    async def collect_evidence(self, url: str, asin: str):
        return await collect_reviews(
            Product(asin=asin, title=f"Amazon product {asin}", url=url), self.config
        )

    # Check prompt/cost bounds without sending a request.
    def prepare_llm_request(self, product: Product, reviews: list[Review]):
        return prepare_request(self.config, product, reviews)

    # Default HTTPX adapter; the Lab overrides this hook without replacing orchestration.
    async def judge_reviews(self, product: Product, reviews: list[Review], charge: AttemptCharge):
        return await analyze_reviews(self.config, product, reviews, charge, self.store)
