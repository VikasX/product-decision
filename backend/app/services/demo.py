import hashlib
import json
from pathlib import Path
from typing import Literal

from app.schemas.demo import DemoCategory, DemoDataset, DemoProduct
from app.services.analysis import AnalysisService
from app.services.usage import PROMPT_VERSION

PRODUCT_ROOT = Path(__file__).resolve().parents[2] / "product"
Workflow = Literal["httpx", "langchain"]


class DemoCatalog:
    """Only explicitly listed, validated datasets appear in the public catalog."""

    def __init__(self, root: Path = PRODUCT_ROOT):
        self.root = root.resolve()

    def safe_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Dataset path must stay within product folder")
        return path

    def datasets(self) -> list[tuple[str, DemoDataset]]:
        manifest = json.loads(self.safe_path("catalog.json").read_text(encoding="utf-8"))
        result = []
        seen = set()
        for category in manifest["categories"]:
            for relative in category["datasets"]:
                dataset = DemoDataset.model_validate_json(
                    self.safe_path(relative).read_text(encoding="utf-8")
                )
                if dataset.category_id != category["id"] or dataset.id in seen:
                    raise ValueError("Catalog identity mismatch or duplicate product")
                seen.add(dataset.id)
                result.append((category["name"], dataset))
        return result

    def get(self, product_id: str) -> DemoDataset:
        for _, dataset in self.datasets():
            if dataset.id == product_id:
                return dataset
        raise KeyError(product_id)

    def categories(self) -> list[DemoCategory]:
        categories = {}
        for name, dataset in self.datasets():
            item = categories.setdefault(
                dataset.category_id,
                DemoCategory(id=dataset.category_id, name=name, product_count=0),
            )
            item.product_count += 1
        return list(categories.values())

    def products(self, category_id: str) -> list[DemoProduct]:
        return [
            DemoProduct(
                id=d.id,
                category_id=d.category_id,
                version=d.version,
                title=d.product.title,
                price_text=d.product.price_text,
                overall_rating=d.product.overall_rating,
                rating_count=d.product.rating_count,
                image_path=f"/api/v1/demo/products/{d.id}/image" if d.image_file else None,
            )
            for _, d in self.datasets()
            if d.category_id == category_id
        ]


class DemoAnalysisService(AnalysisService):
    """Reuse the original report/cost/cache flow, replacing only its evidence source."""

    def __init__(self, config, *, store, dataset: DemoDataset, workflow: Workflow = "httpx"):
        super().__init__(config, store=store)
        self.dataset = dataset
        self.workflow = workflow

    def cache_key(self, asin: str) -> str:
        # Content hashing also invalidates stale results if somebody forgets to bump version.
        digest = hashlib.sha256(self.dataset.model_dump_json().encode()).hexdigest()[:20]
        return (
            f"demo:{self.dataset.id}:v{self.dataset.version}:{digest}:"
            f"{self.config.openai_model}:{PROMPT_VERSION}:{self.workflow}"
        )

    async def collect_evidence(self, url: str, asin: str):
        # No Amazon calls, browser fallback or live data merging occurs in Demo Mode.
        return (
            self.dataset.product.model_copy(deep=True),
            [review.model_copy(deep=True) for review in self.dataset.reviews],
            list(self.dataset.limitations),
        )

    def prepare_llm_request(self, product, reviews):
        if self.workflow == "langchain":
            from app.integrations.langchain_llm import prepare_langchain_request

            return prepare_langchain_request(self.config, product, reviews)
        return super().prepare_llm_request(product, reviews)

    async def judge_reviews(self, product, reviews, charge):
        if self.workflow == "langchain":
            from app.integrations.langchain_llm import analyze_reviews_langchain

            return await analyze_reviews_langchain(
                self.config, product, reviews, charge, self.store
            )
        return await super().judge_reviews(product, reviews, charge)
