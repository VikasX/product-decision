from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.analysis import AnalysisRequest, Product, Review


class ScreenshotExcerpt(BaseModel):
    """A visible topic snippet is not a complete, identifiable written review."""

    # Reject unexpected fields so a manually edited fixture cannot silently add unsupported metadata.
    model_config = ConfigDict(extra="forbid")
    # Keep the topic label and visible words together for tracing the snippet back to its screenshot.
    topic: str
    text: str
    # This relative path must also appear in the dataset's declared source_files list.
    source_file: str
    # Topic panels often truncate quotes; true means we must not treat this as a complete written review.
    partial: bool = True


class DemoDataset(BaseModel):
    # This model validates saved JSON before the catalog or an analysis request can use it.
    model_config = ConfigDict(extra="forbid")
    # IDs are global API identifiers: Gesto and headphones cannot both reuse prd1.
    id: str = Field(pattern=r"^prd[1-9][0-9]*$")
    # Bump the version when evidence changes; it also participates in the analysis cache key.
    version: int = Field(ge=1)
    # Match the category ID in catalog.json; folder names alone do not assign the category.
    category_id: str = Field(pattern=r"^[a-z0-9-]+$")
    # Saved screenshots are the only accepted source type for this demo dataset format.
    source: Literal["user_screenshots"] = "user_screenshots"
    # Optional relative product-photo path for the card; null means no supplied image.
    image_file: str | None = None
    # Reuse the existing product schema so the demo and live workflows produce compatible reports.
    product: Product
    # Only identifiable full reviews belong here; default_factory creates a fresh list for each dataset.
    reviews: list[Review] = Field(default_factory=list, max_length=100)
    # Partial topic-panel quotes remain separate from full reviews and their citation IDs.
    topic_excerpts: list[ScreenshotExcerpt] = Field(default_factory=list)
    # Require source provenance and explicit limitations instead of guessing what missing screenshots contained.
    source_files: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)

    # Run after individual fields are parsed, because these checks compare multiple parts of the dataset.
    @model_validator(mode="after")
    def consistent_evidence(self):
        # Reuse the existing URL boundary and reject contradictory hand-entered evidence.
        canonical = AnalysisRequest(url=self.product.url).url
        if canonical != self.product.url or canonical.rsplit("/", 1)[-1] != self.product.asin:
            raise ValueError("Product identity must match its canonical URL")
        # A repeated star bucket would make percentage calculations ambiguous.
        stars = [bucket.stars for bucket in self.product.rating_distribution]
        if len(stars) != len(set(stars)):
            raise ValueError("Duplicate rating buckets")
        # Compare counts only when all three were actually supplied; missing values must remain unknown.
        for topic in self.product.feedback_topics:
            if all(
                n is not None
                for n in (topic.mention_count, topic.positive_count, topic.negative_count)
            ):
                # Positive and negative mentions must add up to the displayed total for this topic.
                if topic.positive_count + topic.negative_count != topic.mention_count:
                    raise ValueError("Topic counts disagree")
        # Sets remove duplicates, so a shorter set means review IDs cannot uniquely identify their evidence.
        if len({review.id for review in self.reviews}) != len(self.reviews):
            raise ValueError("Duplicate review IDs")
        # Every partial quote must name a declared screenshot rather than an invented source.
        if any(excerpt.source_file not in self.source_files for excerpt in self.topic_excerpts):
            raise ValueError("Unknown screenshot source")
        # Pydantic expects the validated model back from an after-validator.
        return self


class DemoCategory(BaseModel):
    # Small category-tab response: stable ID, readable label and number of enabled products.
    id: str
    name: str
    product_count: int


class DemoProduct(BaseModel):
    # Small product-card response. Nullable price/rating/image fields stay unavailable instead of being invented.
    id: str
    category_id: str
    version: int
    title: str
    price_text: str | None
    overall_rating: float | None
    rating_count: int | None
    image_path: str | None
