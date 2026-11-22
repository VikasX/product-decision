import re
from datetime import date as Date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Verdict = Literal[
    "worth_considering", "proceed_with_caution", "not_recommended", "insufficient_evidence"
]
IssueCategory = Literal[
    "product_defect", "delivery", "subjective_preference", "missing_feature", "usability", "other"
]


class AnalysisRequest(BaseModel):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_product_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", parsed.path, re.I)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"amazon.in", "www.amazon.in"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or not match
        ):
            raise ValueError("Enter an https://www.amazon.in/dp/ASIN product URL")
        return f"https://www.amazon.in/dp/{match.group(1).upper()}"


class RatingBucket(BaseModel):
    stars: Literal[1, 2, 3, 4, 5]
    percentage: float | None = Field(default=None, ge=0, le=100)


class FeedbackTopic(BaseModel):
    name: str
    sentiment: Literal["positive", "negative", "mixed"] | None = None
    mention_count: int | None = Field(default=None, ge=0)
    positive_count: int | None = Field(default=None, ge=0)
    negative_count: int | None = Field(default=None, ge=0)
    text: str | None = None


class Product(BaseModel):
    asin: str
    title: str
    url: str
    overall_rating: float | None = Field(default=None, ge=1, le=5)
    rating_count: int | None = Field(default=None, ge=0)
    rating_distribution: list[RatingBucket] = Field(default_factory=list, max_length=5)
    image_url: str | None = None
    price_text: str | None = None
    specifications: list[dict[str, str]] = Field(default_factory=list)
    customers_say: str | None = None
    feedback_topics: list[FeedbackTopic] = Field(default_factory=list)


class Review(BaseModel):
    id: str
    text: str
    stars: float | None = Field(default=None, ge=1, le=5)
    date: str | None = None
    variant: str | None = None
    source_url: str
    title: str | None = None
    date_iso: str | None = None

    @field_validator("date_iso")
    @classmethod
    def validate_date_iso(cls, value):
        return Date.fromisoformat(value).isoformat() if value is not None else None


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    review_ids: list[str] = Field(min_length=1, max_length=10)


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=240)


class Finding(Reason):
    evidence: list[EvidenceQuote] = Field(min_length=1, max_length=3)


class IssueFinding(Finding):
    category: IssueCategory


class RecurringIssue(IssueFinding):
    mention_count: int = Field(ge=2)


class AmazonFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    sentiment: Literal["positive", "negative", "mixed"]
    source: Literal["customers_say", "topic"]
    topic: str | None
    quote: str = Field(min_length=1, max_length=240)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Verdict
    summary: str = Field(min_length=1, max_length=2000)
    positive_findings: list[Finding] = Field(max_length=3)
    negative_findings: list[Finding] = Field(max_length=3)
    recurring_issues: list[IssueFinding] = Field(max_length=3)
    reliability_findings: list[Finding] = Field(max_length=2)
    key_takeaway: str = Field(min_length=1, max_length=500)
    amazon_findings: list[AmazonFinding] = Field(max_length=3)


class SampleStatistics(BaseModel):
    by_star: dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: {str(star): 0 for star in range(1, 6)}
    )
    negative_count: int = Field(default=0, ge=0)
    positive_count: int = Field(default=0, ge=0)
    neutral_count: int = Field(default=0, ge=0)
    unrated_count: int = Field(default=0, ge=0)
    oldest_review_date: str | None = None
    newest_review_date: str | None = None


class Coverage(BaseModel):
    reviews_analyzed: int
    limitations: list[str]


class Usage(BaseModel):
    model: str
    reserved_cost_usd: float
    daily_reserved_usd: float
    lifetime_reserved_usd: float
    paid_attempts_today: int
    cached: bool = False


class AnalysisResponse(BaseModel):
    status: Literal["completed", "insufficient_evidence"]
    product: Product
    verdict: Verdict
    summary: str
    reasons: list[Reason]
    reviews: list[Review]
    coverage: Coverage
    cached: bool = False
    usage: Usage | None = None
    positive_findings: list[Finding] = Field(default_factory=list)
    negative_findings: list[Finding] = Field(default_factory=list)
    recurring_issues: list[RecurringIssue] = Field(default_factory=list)
    sample_statistics: SampleStatistics = Field(default_factory=SampleStatistics)
    reliability_findings: list[Finding] = Field(default_factory=list)
    key_takeaway: str | None = None
    amazon_findings: list[AmazonFinding] = Field(default_factory=list)

    @field_validator("verdict", mode="before")
    @classmethod
    def migrate_legacy_verdict(cls, value):
        # Old cache entries stay readable; new model judgments use the strict four-value enum.
        return {"buy": "worth_considering", "skip": "not_recommended"}.get(value, value)
