from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.analysis import AnalysisRequest, Product, Review


class ScreenshotExcerpt(BaseModel):
    """A visible topic snippet is not a complete, identifiable written review."""

    model_config = ConfigDict(extra="forbid")
    topic: str
    text: str
    source_file: str
    partial: bool = True


class DemoDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^prd[1-9][0-9]*$")
    version: int = Field(ge=1)
    category_id: str = Field(pattern=r"^[a-z0-9-]+$")
    source: Literal["user_screenshots"] = "user_screenshots"
    image_file: str | None = None
    product: Product
    reviews: list[Review] = Field(default_factory=list, max_length=100)
    topic_excerpts: list[ScreenshotExcerpt] = Field(default_factory=list)
    source_files: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_evidence(self):
        # Reuse the existing URL boundary and reject contradictory hand-entered evidence.
        canonical = AnalysisRequest(url=self.product.url).url
        if canonical != self.product.url or canonical.rsplit("/", 1)[-1] != self.product.asin:
            raise ValueError("Product identity must match its canonical URL")
        stars = [bucket.stars for bucket in self.product.rating_distribution]
        if len(stars) != len(set(stars)):
            raise ValueError("Duplicate rating buckets")
        for topic in self.product.feedback_topics:
            if all(
                n is not None
                for n in (topic.mention_count, topic.positive_count, topic.negative_count)
            ):
                if topic.positive_count + topic.negative_count != topic.mention_count:
                    raise ValueError("Topic counts disagree")
        if len({review.id for review in self.reviews}) != len(self.reviews):
            raise ValueError("Duplicate review IDs")
        if any(excerpt.source_file not in self.source_files for excerpt in self.topic_excerpts):
            raise ValueError("Unknown screenshot source")
        return self


class DemoCategory(BaseModel):
    id: str
    name: str
    product_count: int


class DemoProduct(BaseModel):
    id: str
    category_id: str
    version: int
    title: str
    price_text: str | None
    overall_rating: float | None
    rating_count: int | None
    image_path: str | None
