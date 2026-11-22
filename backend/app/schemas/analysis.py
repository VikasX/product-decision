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


# Public input boundary; canonical URLs also prevent tracking parameters from splitting caches.
class AnalysisRequest(BaseModel):
    # The incoming link is limited to 2,048 characters; the validator below returns its safe canonical form.
    url: str = Field(max_length=2048)

    # Pydantic runs this decorated method while validating a request, review or cached response.
    @field_validator("url")
    # Receive the model class as cls because validation may run before an instance exists.
    @classmethod
    # Accept only HTTPS Amazon India product paths, then return a canonical /dp/ASIN URL.
    def validate_product_url(cls, value: str) -> str:
        # Split the submitted URL before checking its protocol, host, credentials and product path.
        parsed = urlsplit(value.strip())
        # Find a ten-character product ID only in supported /dp/ or /gp/product/ paths.
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
        # Return a stable canonical URL; tracking parameters no longer affect extraction/cache keys.
        return f"https://www.amazon.in/dp/{match.group(1).upper()}"


# Explicit listing percentage: None means unavailable, not zero.
class RatingBucket(BaseModel):
    # The source rating for this item. Validation rejects values outside the permitted star range.
    stars: Literal[1, 2, 3, 4, 5]
    # An explicitly observed share of overall ratings; missing stays None rather than becoming zero.
    percentage: float | None = Field(default=None, ge=0, le=100)


# Amazon AI topic mentions are source metadata, not measured product failure rates.
class FeedbackTopic(BaseModel):
    # The topic name as Amazon supplied it, so findings can refer back to this exact source.
    name: str
    # Only positive, negative or mixed is accepted; optional source fields remain unknown when absent.
    sentiment: Literal["positive", "negative", "mixed"] | None = None
    # How many mentions support this item; topic mentions and directly cited review counts have different meanings.
    mention_count: int | None = Field(default=None, ge=0)
    # Optional positive topic mentions, or collected 4+5 star counts in SampleStatistics.
    positive_count: int | None = Field(default=None, ge=0)
    # Optional negative topic mentions, or collected 1+2 star counts in SampleStatistics.
    negative_count: int | None = Field(default=None, ge=0)
    # The source review or finding wording; Field limits below keep model text bounded where specified.
    text: str | None = None


# Observed listing evidence shared by extraction, both adapters and the UI.
class Product(BaseModel):
    # The ten-character Amazon product identifier used to connect URLs, reports and cache keys.
    asin: str
    # Observed source title, retained for display; optional review titles may be missing.
    title: str
    # Canonical Amazon product URL created by request validation and retained for source links.
    url: str
    # The listing average rating, validated between one and five, not recalculated from our small sample.
    overall_rating: float | None = Field(default=None, ge=1, le=5)
    # The listing total ratings count, not the number of written reviews we collected.
    rating_count: int | None = Field(default=None, ge=0)
    # Keep up to five explicit star buckets. default_factory gives each new product its own empty list.
    rating_distribution: list[RatingBucket] = Field(default_factory=list, max_length=5)
    # Optional observed product image URL; extraction separately checks its permitted host.
    image_url: str | None = None
    # Optional observed price text for display; no price is guessed when Amazon omits it.
    price_text: str | None = None
    # Observed label/value pairs for the UI. Each new product gets its own list.
    specifications: list[dict[str, str]] = Field(default_factory=list)
    # Optional Amazon AI-generated customer summary, kept separate from direct written reviews.
    customers_say: str | None = None
    # Observed Amazon feedback topics with sentiment/counts where available.
    feedback_topics: list[FeedbackTopic] = Field(default_factory=list)


# Direct written evidence retains source IDs/text for later citation checks.
class Review(BaseModel):
    # Stable review identifier used by findings and quote checks to locate the source text.
    id: str
    # The source review or finding wording; Field limits below keep model text bounded where specified.
    text: str
    # The source rating for this item. Validation rejects values outside the permitted star range.
    stars: float | None = Field(default=None, ge=1, le=5)
    # The original human-readable review-date label, if the page provides one.
    date: str | None = None
    # Observed product variant text so users can see when reviews may describe a different variation.
    variant: str | None = None
    # Amazon source link retained for the UI evidence links; it is not generated by the model.
    source_url: str
    # Observed source title, retained for display; optional review titles may be missing.
    title: str | None = None
    # Recognized review date in YYYY-MM-DD form for sorting and sample-date boundaries.
    date_iso: str | None = None

    # Pydantic runs this decorated method while validating a request, review or cached response.
    @field_validator("date_iso")
    # Receive the model class as cls because validation may run before an instance exists.
    @classmethod
    def validate_date_iso(cls, value):
        # Check that a supplied ISO date is a real calendar date, preserving None when unavailable.
        return Date.fromisoformat(value).isoformat() if value is not None else None


class Reason(BaseModel):
    # Reject extra fields in this model rather than silently accepting unsupported model output.
    model_config = ConfigDict(extra="forbid")
    # The source review or finding wording; Field limits below keep model text bounded where specified.
    text: str = Field(min_length=1, max_length=1000)
    # Nonempty source IDs supporting a finding; later evidence checks confirm they exist and are distinct.
    review_ids: list[str] = Field(min_length=1, max_length=10)


class EvidenceQuote(BaseModel):
    # Reject extra fields in this model rather than silently accepting unsupported model output.
    model_config = ConfigDict(extra="forbid")
    # The specific supplied review containing this supporting excerpt.
    review_id: str = Field(min_length=1)
    # A short source excerpt. A separate check requires it to occur in the cited review, summary or topic.
    quote: str = Field(min_length=1, max_length=240)


class Finding(Reason):
    # Short quotes supporting this finding; limits prevent excessive repeated source text.
    evidence: list[EvidenceQuote] = Field(min_length=1, max_length=3)


class IssueFinding(Finding):
    # Use the fixed issue categories so defects, delivery and personal preferences remain distinguishable.
    category: IssueCategory


class RecurringIssue(IssueFinding):
    # The service derives this count from distinct cited review IDs; it must be at least two.
    mention_count: int = Field(ge=2)


# Secondary Amazon AI evidence has its own source and quote, separate from review citations.
class AmazonFinding(BaseModel):
    # Reject extra fields in this model rather than silently accepting unsupported model output.
    model_config = ConfigDict(extra="forbid")
    # The source review or finding wording; Field limits below keep model text bounded where specified.
    text: str = Field(min_length=1, max_length=500)
    # Only positive, negative or mixed is accepted; optional source fields remain unknown when absent.
    sentiment: Literal["positive", "negative", "mixed"]
    # Choose whether this finding cites the Amazon summary or a named Amazon topic.
    source: Literal["customers_say", "topic"]
    # Exact topic name for topic evidence, or None for a Customers Say summary finding.
    topic: str | None
    # A short source excerpt. A separate check requires it to occur in the cited review, summary or topic.
    quote: str = Field(min_length=1, max_length=240)


# Strict LLM-output schema shared by both adapters; the service adds API metadata afterward.
class Judgment(BaseModel):
    # Reject extra fields in this model rather than silently accepting unsupported model output.
    model_config = ConfigDict(extra="forbid")
    # One of the four allowed buying conclusions; missing evidence has its own explicit verdict.
    verdict: Verdict
    # The explanation shown beside the verdict, with a maximum length on model-generated output.
    summary: str = Field(min_length=1, max_length=2000)
    # Source-backed strengths; empty means no supported claims, not proof that nothing is good.
    positive_findings: list[Finding] = Field(max_length=3)
    # Source-backed concerns kept separate from positive findings.
    negative_findings: list[Finding] = Field(max_length=3)
    # Repeated direct-review concerns; evidence validation requires at least two unique supporting reviews.
    recurring_issues: list[IssueFinding] = Field(max_length=3)
    # Supported reliability observations; these do not replace long-term product testing.
    reliability_findings: list[Finding] = Field(max_length=2)
    # Short practical buying guidance, optional for older saved/API reports.
    key_takeaway: str = Field(min_length=1, max_length=500)
    # Claims supported by Amazon AI summary/topic evidence, using their own source quotes.
    amazon_findings: list[AmazonFinding] = Field(max_length=3)


class SampleStatistics(BaseModel):
    # Counts of exact 1-to-5 star ratings in the collected written-review sample.
    by_star: dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: {str(star): 0 for star in range(1, 6)}
    )
    # Optional negative topic mentions, or collected 1+2 star counts in SampleStatistics.
    negative_count: int = Field(default=0, ge=0)
    # Optional positive topic mentions, or collected 4+5 star counts in SampleStatistics.
    positive_count: int = Field(default=0, ge=0)
    # Number of collected written reviews rated exactly three stars.
    neutral_count: int = Field(default=0, ge=0)
    # Collected reviews without one of the five exact star ratings remain in this separate bucket.
    unrated_count: int = Field(default=0, ge=0)
    # Earliest recognized review date in the collected sample, or None if none were recognized.
    oldest_review_date: str | None = None
    # Latest recognized review date in the collected sample; it does not describe all Amazon reviews.
    newest_review_date: str | None = None


class Coverage(BaseModel):
    # How many collected reviews were actually sent in a successfully validated model analysis.
    reviews_analyzed: int
    # Source/analysis limitations shown to the user so unavailable evidence is not mistaken for certainty.
    limitations: list[str]


class Usage(BaseModel):
    # The configured allowed model name used for this analysis and reservation reporting.
    model: str
    # Conservative maximum cost reserved for this attempt, not a finalized OpenAI charge.
    reserved_cost_usd: float
    # Sum of today reservations in this store; day boundaries follow Asia/Calcutta.
    daily_reserved_usd: float
    # Sum of all reservations in this store, including failed attempts.
    lifetime_reserved_usd: float
    # Number of reserved attempts today; failed calls can still count.
    paid_attempts_today: int
    # Tell the UI whether this report reused storage rather than making a new model call.
    cached: bool = False


# Public report combines validated judgment, source data and deterministic coverage/cache/usage.
class AnalysisResponse(BaseModel):
    # Distinguish completed analysis from insufficient evidence using a fixed response value.
    status: Literal["completed", "insufficient_evidence"]
    # The observed product metadata and secondary Amazon summary/topic evidence.
    product: Product
    # One of the four allowed buying conclusions; missing evidence has its own explicit verdict.
    verdict: Verdict
    # The explanation shown beside the verdict, with a maximum length on model-generated output.
    summary: str
    # Compatibility list of supported verdict reasons built by the service from validated findings.
    reasons: list[Reason]
    # Collected direct written sources used for citations and user inspection.
    reviews: list[Review]
    # Keep analysis count and limitations together so the UI can show evidence quality.
    coverage: Coverage
    # Tell the UI whether this report reused storage rather than making a new model call.
    cached: bool = False
    # Optional budget reservation information; older cached reports may omit it.
    usage: Usage | None = None
    # Source-backed strengths; empty means no supported claims, not proof that nothing is good.
    positive_findings: list[Finding] = Field(default_factory=list)
    # Source-backed concerns kept separate from positive findings.
    negative_findings: list[Finding] = Field(default_factory=list)
    # Repeated direct-review concerns; evidence validation requires at least two unique supporting reviews.
    recurring_issues: list[RecurringIssue] = Field(default_factory=list)
    # Deterministic Python counts/date bounds for collected reviews, separate from listing percentages.
    sample_statistics: SampleStatistics = Field(default_factory=SampleStatistics)
    # Supported reliability observations; these do not replace long-term product testing.
    reliability_findings: list[Finding] = Field(default_factory=list)
    # Short practical buying guidance, optional for older saved/API reports.
    key_takeaway: str | None = None
    # Claims supported by Amazon AI summary/topic evidence, using their own source quotes.
    amazon_findings: list[AmazonFinding] = Field(default_factory=list)

    # Pydantic runs this decorated method while validating a request, review or cached response.
    @field_validator("verdict", mode="before")
    # Receive the model class as cls because validation may run before an instance exists.
    @classmethod
    def migrate_legacy_verdict(cls, value):
        # Old cache entries stay readable; new model judgments use the strict four-value enum.
        return {"buy": "worth_considering", "skip": "not_recommended"}.get(value, value)
