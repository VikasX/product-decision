from app.integrations.langchain_llm import analyze_reviews_langchain, prepare_langchain_request
from app.integrations.llm import AttemptCharge
from app.schemas.analysis import Product, Review
from app.services.analysis import AnalysisService
from app.services.usage import PROMPT_VERSION


class LangChainAnalysisService(AnalysisService):
    """Independent judgment/cache, shared collection, response assembly and spending ledger."""

    cache_namespace = ":langchain-v1"

    async def collect_evidence(self, url: str, asin: str):
        http_key = f"amazon.in:{asin}:{self.config.openai_model}:{PROMPT_VERSION}"
        cached_http = self.store.cached(http_key)
        if cached_http is not None and cached_http.reviews:
            return (
                cached_http.product.model_copy(deep=True),
                [review.model_copy(deep=True) for review in cached_http.reviews],
                [
                    *cached_http.coverage.limitations,
                    "Review sample reused from cached HTTP collection; "
                    "LangChain produces its own independent judgment.",
                ],
            )
        # One existing bounded collection pass. No extra fetching or follow-up loop.
        return await super().collect_evidence(url, asin)

    def prepare_llm_request(self, product: Product, reviews: list[Review]):
        return prepare_langchain_request(self.config, product, reviews)

    async def judge_reviews(self, product: Product, reviews: list[Review], charge: AttemptCharge):
        return await analyze_reviews_langchain(self.config, product, reviews, charge, self.store)
