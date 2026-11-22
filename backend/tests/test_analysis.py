import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.amazon import parse_page
from app.integrations.llm import analyze_reviews
from app.main import create_app
from app.schemas.analysis import AnalysisRequest, Product, Review


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/dp/B0D5Q7RWF6",
        "https://www.amazon.in.evil.com/dp/B0D5Q7RWF6",
        "https://www.amazon.in:123/dp/B0D5Q7RWF6",
        "http://www.amazon.in/dp/B0D5Q7RWF6",
    ],
)
def test_rejects_unsupported_urls(url):
    with pytest.raises(ValidationError):
        AnalysisRequest(url=url)


def test_no_reviews_does_not_spend_credit_and_caches():
    app = create_app(Settings(_env_file=None, mongodb_uri="mongodb://localhost/"))
    product = Product(
        asin="B0D5Q7RWF6", title="LED strip", url="https://www.amazon.in/dp/B0D5Q7RWF6"
    )

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                first = await client.post("/api/v1/analysis", json={"url": product.url + "?th=1"})
                second = await client.post("/api/v1/analysis", json={"url": product.url})
                return first, second

    with (
        patch(
            "app.services.analysis.collect_reviews",
            AsyncMock(return_value=(product, [], ["CAPTCHA"])),
        ) as collect,
        patch("app.services.analysis.analyze_reviews", AsyncMock()) as llm,
    ):
        first, second = asyncio.run(run())
    assert first.status_code == 200
    assert first.json()["verdict"] == "insufficient_evidence"
    assert second.json()["cached"] is True
    collect.assert_awaited_once()
    llm.assert_not_awaited()


def test_rejects_invented_review_citations():
    config = Settings(_env_file=None, mongodb_uri="mongodb://localhost/", openai_api_key="test")
    product = Product(asin="B0D5Q7RWF6", title="LED", url="https://www.amazon.in/dp/B0D5Q7RWF6")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.openai.com"),
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "verdict": "proceed_with_caution",
                                "summary": "Bad colours",
                                "positive_findings": [],
                                "amazon_findings": [],
                                "recurring_issues": [],
                                "reliability_findings": [],
                                "key_takeaway": "Check the cited evidence before buying.",
                                "negative_findings": [
                                    {
                                        "text": "Colour failures",
                                        "review_ids": ["invented"],
                                        "evidence": [
                                            {"review_id": "invented", "quote": "Only blue works"}
                                        ],
                                    }
                                ],
                            }
                        )
                    }
                }
            ],
        },
    )
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=response)):
        with pytest.raises(ValueError, match="unverified"):
            asyncio.run(
                analyze_reviews(
                    config,
                    product,
                    [Review(id="real", text="Only blue works", stars=1, source_url=product.url)],
                )
            )


def test_openai_auth_failure_preserves_reviews_without_exposing_key():
    from app.services.analysis import AnalysisService

    service = AnalysisService(Settings(_env_file=None, mongodb_uri="mongodb://localhost/"))
    product = Product(asin="B0D5Q7RWF6", title="LED", url="https://www.amazon.in/dp/B0D5Q7RWF6")
    reviews = [
        Review(id=str(index), text=f"LED review {index}", stars=3, source_url=product.url)
        for index in range(3)
    ]
    error = httpx.HTTPStatusError(
        "private upstream details",
        request=httpx.Request("POST", "https://api.openai.com"),
        response=httpx.Response(401),
    )
    with (
        patch(
            "app.services.analysis.collect_reviews", AsyncMock(return_value=(product, reviews, []))
        ),
        patch("app.services.analysis.analyze_reviews", AsyncMock(side_effect=error)),
    ):
        result = asyncio.run(service.analyze(product.url))
    assert "rejected the API key" in result.summary
    assert len(result.reviews) == 3
    assert result.verdict == "insufficient_evidence"
    assert "private upstream details" not in result.model_dump_json()


@pytest.mark.parametrize("body_hook", ["review-body", "reviewRichContentContainer"])
def test_extracts_written_reviews_without_instructions(body_hook):
    product = Product(asin="B0D5Q7RWF6", title="LED", url="https://www.amazon.in/dp/B0D5Q7RWF6")
    title, reviews = parse_page(
        f"""
        <span id="productTitle">LED strip</span>
        <div data-hook="review" id="R123">
          <i data-hook="review-star-rating">1.0 out of 5 stars</i>
          <span data-hook="{body_hook}">Only blue works</span>
          <span data-hook="review-date">20 November 2026</span>
        </div>""",
        product,
    )
    assert title == "LED strip"
    assert reviews[0].stars == 1
    assert reviews[0].source_url.endswith("R123")
