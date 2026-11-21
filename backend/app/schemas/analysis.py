import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class Product(BaseModel):
    asin: str
    title: str
    url: str


class Review(BaseModel):
    id: str
    text: str
    stars: float | None = None
    date: str | None = None
    variant: str | None = None
    source_url: str


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    review_ids: list[str] = Field(min_length=1, max_length=10)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["buy", "skip", "insufficient_evidence"]
    summary: str = Field(min_length=1, max_length=2000)
    reasons: list[Reason] = Field(min_length=1, max_length=5)


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
    verdict: Literal["buy", "skip", "insufficient_evidence"]
    summary: str
    reasons: list[Reason]
    reviews: list[Review]
    coverage: Coverage
    cached: bool = False
    usage: Usage | None = None
