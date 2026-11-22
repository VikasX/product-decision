import hashlib
import json
from pathlib import Path
from typing import Literal

from app.schemas.demo import DemoCategory, DemoDataset, DemoProduct
from app.services.analysis import AnalysisService
from app.services.usage import PROMPT_VERSION

# Resolve from this file, so saved products can be found regardless of the server's working directory.
PRODUCT_ROOT = Path(__file__).resolve().parents[2] / "product"
# Literal limits the API's workflow query parameter to the two existing adapters.
Workflow = Literal["httpx", "langchain"]


class DemoCatalog:
    """Only explicitly listed, validated datasets appear in the public catalog."""

    def __init__(self, root: Path = PRODUCT_ROOT):
        # Tests can supply a different folder; normal requests use the bundled product folder.
        self.root = root.resolve()

    def safe_path(self, relative: str) -> Path:
        # Resolve '..' before checking the boundary, so a dataset cannot point outside our product folder.
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Dataset path must stay within product folder")
        return path

    def datasets(self) -> list[tuple[str, DemoDataset]]:
        # The manifest explicitly lists enabled datasets. Extra folders are not automatically published.
        manifest = json.loads(self.safe_path("catalog.json").read_text(encoding="utf-8"))
        # Each output entry pairs the category's readable name with a validated product dataset.
        result = []
        # Product IDs must be unique across categories because API routes look up products by ID alone.
        seen = set()
        # Visit only the category paths listed in catalog.json, keeping their display order.
        for category in manifest["categories"]:
            for relative in category["datasets"]:
                # Pydantic checks the saved fields and their consistency before any card or analysis uses them.
                dataset = DemoDataset.model_validate_json(
                    self.safe_path(relative).read_text(encoding="utf-8")
                )
                # Reject a wrong category or reused ID instead of showing an ambiguous product.
                if dataset.category_id != category["id"] or dataset.id in seen:
                    raise ValueError("Catalog identity mismatch or duplicate product")
                # Remember this ID before processing the next file.
                seen.add(dataset.id)
                result.append((category["name"], dataset))
        return result

    def get(self, product_id: str) -> DemoDataset:
        # Input: the public product ID. Output: its full saved evidence, or KeyError for the route to turn into 404.
        for _, dataset in self.datasets():
            if dataset.id == product_id:
                return dataset
        raise KeyError(product_id)

    def categories(self) -> list[DemoCategory]:
        # Group the validated products by category rather than displaying empty categories.
        categories = {}
        for name, dataset in self.datasets():
            # setdefault creates the category once; later products reuse and increment the same object.
            item = categories.setdefault(
                dataset.category_id,
                DemoCategory(id=dataset.category_id, name=name, product_count=0),
            )
            # The UI badge counts datasets, not reviews or analysis attempts.
            item.product_count += 1
        return list(categories.values())

    def products(self, category_id: str) -> list[DemoProduct]:
        # Return small card records for one category. Full summaries and source excerpts have their own endpoint.
        return [
            DemoProduct(
                id=d.id,
                category_id=d.category_id,
                version=d.version,
                title=d.product.title,
                price_text=d.product.price_text,
                overall_rating=d.product.overall_rating,
                rating_count=d.product.rating_count,
                # Send an API image path, not a private filesystem location. Missing images remain null.
                image_path=f"/api/v1/demo/products/{d.id}/image" if d.image_file else None,
            )
            for _, d in self.datasets()
            if d.category_id == category_id
        ]


class DemoAnalysisService(AnalysisService):
    """Reuse the original report/cost/cache flow, replacing only its evidence source."""

    def __init__(self, config, *, store, dataset: DemoDataset, workflow: Workflow = "httpx"):
        # Inherit the normal cache, lease, validation and spending flow with the existing shared store.
        super().__init__(config, store=store)
        # Keep this request's validated evidence and selected adapter; neither performs network work here.
        self.dataset = dataset
        self.workflow = workflow

    def cache_key(self, asin: str) -> str:
        # Content hashing also invalidates stale results if somebody forgets to bump version.
        digest = hashlib.sha256(self.dataset.model_dump_json().encode()).hexdigest()[:20]
        # Include product, evidence version/content, model, prompt and adapter so unrelated reports never collide.
        return (
            f"demo:{self.dataset.id}:v{self.dataset.version}:{digest}:"
            f"{self.config.openai_model}:{PROMPT_VERSION}:{self.workflow}"
        )

    async def collect_evidence(self, url: str, asin: str):
        # No Amazon calls, browser fallback or live data merging occurs in Demo Mode.
        # async matches the parent's awaited collector. Deep copies keep later processing from editing the dataset.
        return (
            self.dataset.product.model_copy(deep=True),
            [review.model_copy(deep=True) for review in self.dataset.reviews],
            list(self.dataset.limitations),
        )

    def prepare_llm_request(self, product, reviews):
        # Build the payload and cost estimate without calling a model. Only the chosen adapter changes.
        if self.workflow == "langchain":
            # Load the optional LangChain integration only when that workflow is actually selected.
            from app.integrations.langchain_llm import prepare_langchain_request

            return prepare_langchain_request(self.config, product, reviews)
        return super().prepare_llm_request(product, reviews)

    async def judge_reviews(self, product, reviews, charge):
        # await pauses this request while the adapter runs; both paths use the same budget and evidence checks.
        if self.workflow == "langchain":
            from app.integrations.langchain_llm import analyze_reviews_langchain

            return await analyze_reviews_langchain(
                self.config, product, reviews, charge, self.store
            )
        return await super().judge_reviews(product, reviews, charge)
