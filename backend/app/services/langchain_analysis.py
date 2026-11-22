from app.integrations.langchain_llm import analyze_reviews_langchain, prepare_langchain_request
from app.integrations.llm import AttemptCharge
from app.schemas.analysis import Product, Review
from app.services.analysis import AnalysisService
from app.services.review_evidence import usable_summary
from app.services.usage import PROMPT_VERSION


class LangChainAnalysisService(AnalysisService):
    """Independent judgment/cache, shared collection, response assembly and spending ledger."""

    # LangChain judgments have separate keys even when source product/review data is shared.
    cache_namespace = ":langchain-v1"

    # Copy HTTP source evidence, not its verdict; avoid extra collection and cached-object mutation.
    async def collect_evidence(self, url: str, asin: str):
        # Look up source evidence under the normal HTTP workflow key, using the same model/evidence version.
        http_key = f"amazon.in:{asin}:{self.config.openai_model}:{PROMPT_VERSION}"
        # Read an unexpired verified HTTP report; its source data may save another Amazon fetch.
        cached_http = self.store.cached(http_key)
        # Reuse it only when direct reviews or a usable summary can support a fresh Lab judgment.
        if cached_http is not None and (cached_http.reviews or usable_summary(cached_http.product)):
            return (
                # Make a deep copy so this workflow cannot accidentally change the saved HTTP product.
                cached_http.product.model_copy(deep=True),
                # Copy each review too; the Lab will create its own verdict and cache entry.
                [review.model_copy(deep=True) for review in cached_http.reviews],
                [
                    *cached_http.coverage.limitations,
                    "Review sample reused from cached HTTP collection; "
                    "LangChain produces its own independent judgment.",
                ],
            )
        # One existing bounded collection pass. No extra fetching or follow-up loop.
        return await super().collect_evidence(url, asin)

    # Reuse prompt content while allowing for additional SDK schema framing.
    def prepare_llm_request(self, product: Product, reviews: list[Review]):
        return prepare_langchain_request(self.config, product, reviews)

    # Only this adapter changes; inherited reporting, limits and validation remain shared.
    async def judge_reviews(self, product: Product, reviews: list[Review], charge: AttemptCharge):
        return await analyze_reviews_langchain(self.config, product, reviews, charge, self.store)
