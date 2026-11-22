import asyncio
import time
import uuid

import httpx

from app.core.config import Settings
from app.integrations.amazon import balanced_review_sample, collect_reviews
from app.integrations.llm import AttemptCharge, analyze_reviews, prepare_request
from app.schemas.analysis import AnalysisResponse, Coverage, Product, Reason, RecurringIssue, Review
from app.services.review_evidence import sample_statistics
from app.services.usage import (
    PROMPT_VERSION,
    CostProtectionError,
    create_usage_store,
    credential_fingerprint,
)


class AnalysisService:
    cache_namespace = ""

    def __init__(self, config: Settings, *, store=None):
        self.config = config
        self.store = store if store is not None else create_usage_store(config)

    async def analyze(self, url: str) -> AnalysisResponse:
        asin = url.rsplit("/", 1)[-1]
        key = f"amazon.in:{asin}:{self.config.openai_model}:{PROMPT_VERSION}{self.cache_namespace}"
        failure_key = f"{key}:failure:{credential_fingerprint(self.config)}"
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + 210
        while time.monotonic() < deadline:
            cached = self.store.cached(key) or self.store.cached(failure_key)
            if cached is not None:
                cached.cached = True
                cached.usage = self.store.usage(cached=True)
                return cached
            if self.store.acquire(key, owner):
                break
            await asyncio.sleep(0.5)
        else:
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
            cached = self.store.cached(key) or self.store.cached(failure_key)
            if cached is not None:
                cached.cached = True
                cached.usage = self.store.usage(cached=True)
                return cached
            result, verified = await self.run_analysis(url, asin)
            self.store.finish(
                key,
                owner,
                key if verified else failure_key,
                result,
                ttl=86400 if verified else 60,
            )
            return result
        finally:
            self.store.release(key, owner)

    async def run_analysis(self, url: str, asin: str) -> tuple[AnalysisResponse, bool]:
        product, reviews, limitations = await self.collect_evidence(url, asin)
        result = AnalysisResponse(
            status="insufficient_evidence",
            product=product,
            verdict="insufficient_evidence",
            summary="Could not collect enough written reviews to support a buying decision.",
            reasons=[],
            reviews=reviews,
            coverage=Coverage(reviews_analyzed=0, limitations=limitations),
            sample_statistics=sample_statistics(reviews),
        )
        charge = AttemptCharge()
        verified = False
        analysis_reviews = self.fit_review_sample(product, reviews)
        if len(analysis_reviews) < len(reviews):
            result.coverage.limitations.append(
                f"AI input limits allow {len(analysis_reviews)} of {len(reviews)} "
                "collected reviews "
                "to be analysed. The subset balances available negative, neutral and "
                "positive evidence."
            )
        if len(analysis_reviews) >= 3:
            try:
                judgment = await self.judge_reviews(product, analysis_reviews, charge)
                result.verdict = judgment.verdict
                result.status = (
                    "insufficient_evidence"
                    if judgment.verdict == "insufficient_evidence"
                    else "completed"
                )
                result.summary = judgment.summary
                result.positive_findings = judgment.positive_findings
                result.negative_findings = judgment.negative_findings
                result.reliability_findings = judgment.reliability_findings
                result.key_takeaway = judgment.key_takeaway
                result.recurring_issues = [
                    RecurringIssue(**issue.model_dump(), mention_count=len(set(issue.review_ids)))
                    for issue in judgment.recurring_issues
                ]
                result.reasons = [
                    Reason(text=finding.text, review_ids=finding.review_ids)
                    for finding in (
                        judgment.negative_findings
                        + judgment.positive_findings
                        + judgment.recurring_issues
                    )
                ]
                result.coverage.reviews_analyzed = len(analysis_reviews)
                verified = True
            except CostProtectionError as exc:
                result.summary = str(exc)
                result.coverage.limitations.append("No new AI request was made.")
            except httpx.HTTPStatusError as exc:
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
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                result.summary = "Reviews were collected, but AI analysis could not be verified."
                result.coverage.limitations.append(
                    "The attempt is counted against the budget. No automatic retry was made."
                )
        else:
            result.coverage.limitations.append(
                "At least three written reviews are required before spending API credit."
            )
        result.usage = self.store.usage(reserved=charge.reserved)
        return result, verified

    def fit_review_sample(self, product: Product, reviews: list[Review]) -> list[Review]:
        if len(reviews) < 3:
            return reviews
        for count in range(len(reviews), 2, -1):
            selected = balanced_review_sample(reviews, count)
            try:
                self.prepare_llm_request(product, selected)
                return selected
            except CostProtectionError:
                continue
        return []

    async def collect_evidence(self, url: str, asin: str):
        return await collect_reviews(
            Product(asin=asin, title=f"Amazon product {asin}", url=url), self.config
        )

    def prepare_llm_request(self, product: Product, reviews: list[Review]):
        return prepare_request(self.config, product, reviews)

    async def judge_reviews(self, product: Product, reviews: list[Review], charge: AttemptCharge):
        return await analyze_reviews(self.config, product, reviews, charge, self.store)
