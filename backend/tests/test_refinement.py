import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.amazon import (
    AccessRestrictedError,
    balanced_review_sample,
    collect_reviews,
    fetch_page,
    next_review_page,
    parse_page,
)
from app.integrations.llm import prepare_request, validate_evidence
from app.main import create_app
from app.schemas.analysis import AnalysisResponse, Judgment, Product, Review
from app.services.analysis import AnalysisService
from app.services.review_evidence import (
    deduplicate_reviews,
    normalize_text,
    parse_review_date,
    sample_statistics,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "amazon_listing.html").read_text(encoding="utf-8")


def product():
    return Product(
        asin="B0D5Q7RWF6", title="Amazon product", url="https://www.amazon.in/dp/B0D5Q7RWF6"
    )


def settings(**overrides):
    return Settings(_env_file=None, mongodb_uri="mongodb://localhost/", **overrides)


def judgment(**overrides):
    data = {
        "verdict": "proceed_with_caution",
        "summary": "Repeated adhesive concerns in this sample.",
        "positive_findings": [],
        "amazon_findings": [],
        "negative_findings": [
            {
                "text": "Adhesive problems",
                "review_ids": ["RNEG1", "RNEG2"],
                "evidence": [
                    {"review_id": "RNEG1", "quote": "Glue fails"},
                    {"review_id": "RNEG2", "quote": "The glue fails after a day"},
                ],
            }
        ],
        "recurring_issues": [
            {
                "text": "Adhesive problems recur",
                "category": "usability",
                "review_ids": ["RNEG1", "RNEG2"],
                "evidence": [
                    {"review_id": "RNEG1", "quote": "Glue fails"},
                    {"review_id": "RNEG2", "quote": "The glue fails after a day"},
                ],
            }
        ],
        "reliability_findings": [],
        "key_takeaway": "Check installation requirements before buying.",
    }
    data.update(overrides)
    return Judgment.model_validate(data)


def test_fixture_extracts_product_histogram_and_complete_review_fields():
    listing = product()
    title, reviews = parse_page(FIXTURE, listing)
    assert title == "Fixture LED strip"
    assert listing.overall_rating == 3.4
    assert listing.rating_count == 373
    assert {bucket.stars: bucket.percentage for bucket in listing.rating_distribution} == {
        5: 45,
        4: 12,
        3: 11,
        2: 6,
        1: 26,
    }
    assert reviews[0].id == "RNEG1"
    assert reviews[0].title == "Colours failed"
    assert reviews[0].text == "Only blue works. Glue fails."
    assert reviews[0].date == "Reviewed in India on 15 November 2026"
    assert reviews[0].date_iso == "2026-11-15"
    assert reviews[0].variant == "Style Name: Bohemian"
    assert reviews[0].source_url.endswith("/RNEG1")


def test_missing_histogram_stars_are_not_invented_as_zero():
    listing = product()
    parse_page('<table id="histogramTable"><tr><td>1 star</td><td>26%</td></tr></table>', listing)
    assert [(bucket.stars, bucket.percentage) for bucket in listing.rating_distribution] == [
        (1, 26)
    ]
    assert listing.overall_rating is None
    assert listing.rating_count is None


def test_malformed_ratings_and_unrelated_sponsored_metadata_are_ignored():
    listing = product()
    html = """<span class="a-icon-alt">4.9 out of 5 stars</span>
    <span id="acrPopover" title="5.9 out of 5 stars"></span>
    <div data-hook="review" id="RBAD">
    <span data-hook="review-body">A review without a valid rating.</span>
    <i data-hook="review-star-rating">6.0 out of 5 stars</i></div>"""
    _, reviews = parse_page(html, listing)
    assert listing.overall_rating is None
    assert reviews[0].stars is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Reviewed in India on 15 November 2026", "2026-11-15"),
        ("2026-11-15", "2026-11-15"),
        ("Reviewed in France on 15 novembre 2026", None),
        ("31 February 2026", None),
        (None, None),
    ],
)
def test_dates_preserve_unknown_formats_without_guessing(raw, expected):
    assert parse_review_date(raw) == expected


def test_normalization_and_deduplication_across_ids_and_pages():
    source = product().url
    reviews = [
        Review(id="RA", text="Good\u00a0  build", stars=4, source_url=source),
        Review(id="RB", text="Ｇｏｏｄ build", stars=4, source_url=source),
        Review(id="RA", text="Different text under repeated ID", stars=4, source_url=source),
        Review(id="RC", text="Colour fails", stars=1, source_url=source),
    ]
    unique = deduplicate_reviews(reviews, 30)
    assert normalize_text("Good\u00a0 build") == "Good build"
    assert [review.id for review in unique] == ["RA", "RC"]
    assert unique[0].text == "Good build"


def test_python_sample_counts_are_distinct_from_listing_rating_distribution():
    listing = product()
    _, reviews = parse_page(FIXTURE, listing)
    stats = sample_statistics(
        reviews + [Review(id="RUNKNOWN", text="Unknown", source_url=listing.url)]
    )
    assert stats.by_star == {"1": 1, "2": 1, "3": 0, "4": 1, "5": 1}
    assert stats.negative_count == stats.positive_count == 2
    assert stats.unrated_count == 1
    assert stats.oldest_review_date == "2026-10-03"
    assert stats.newest_review_date == "2026-11-15"
    assert listing.rating_count == 373


def test_balanced_collection_does_not_backfill_missing_category():
    source = product().url
    reviews = [
        Review(id=f"RNEG{i}", text=f"Defect {i}", stars=1, source_url=source) for i in range(20)
    ] + [Review(id=f"RPOS{i}", text=f"Useful {i}", stars=5, source_url=source) for i in range(20)]
    selected = balanced_review_sample(reviews, 12)
    assert len(selected) == 12
    assert sum(review.stars == 1 for review in selected) == 6
    assert sum(review.stars == 5 for review in selected) == 6
    assert len(balanced_review_sample(reviews, 30)) == 14


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/product-reviews/B0D5Q7RWF6/?pageNumber=2",
        "/product-reviews/B012345678/?pageNumber=2",
        "https://www.amazon.in:444/product-reviews/B0D5Q7RWF6/?pageNumber=2",
        "https://name:password@www.amazon.in/product-reviews/B0D5Q7RWF6/?pageNumber=2",
    ],
)
def test_pagination_does_not_follow_external_or_other_product_links(href):
    html = f'<ul class="a-pagination"><li class="a-last"><a href="{href}">Next</a></li></ul>'
    assert next_review_page(html, product().url, product().asin) is None


def test_pagination_follows_only_observed_same_product_link():
    url = next_review_page(FIXTURE, product().url, product().asin)
    assert (
        url
        == "https://www.amazon.in/product-reviews/B0D5Q7RWF6/?filterByStar=one_star&pageNumber=2"
    )


@pytest.mark.parametrize("status, expected_calls", [(503, 2), (404, 1), (403, 1), (429, 1)])
def test_only_transient_http_failures_are_retried(status, expected_calls):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="Access unavailable")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_page(client, product().url, retries=1)

    asyncio.run(run())
    assert len(calls) == expected_calls


def test_captcha_and_signin_are_never_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='<input id="captchacharacters">')

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AccessRestrictedError):
                await fetch_page(client, product().url, retries=1)

    asyncio.run(run())
    assert len(calls) == 1


def test_collection_requests_listing_only_even_with_large_page_budget(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=FIXTURE)

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(handler))

    with patch("app.integrations.amazon.httpx.AsyncClient", side_effect=client):
        listing, reviews, _ = asyncio.run(collect_reviews(product(), settings(amazon_max_pages=8)))
    assert len(requests) == 1
    assert requests[0].url.path.startswith("/dp/")
    assert not requests[0].url.params
    assert len(reviews) == 4
    assert listing.rating_count == 373


def test_collection_stops_repeating_restricted_review_route(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        if "/dp/" in request.url.path:
            return httpx.Response(200, text=FIXTURE)
        return httpx.Response(302, headers={"location": "https://www.amazon.in/ap/signin"})

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(handler))

    with patch("app.integrations.amazon.httpx.AsyncClient", side_effect=client):
        _, reviews, limits = asyncio.run(collect_reviews(product(), settings()))
    assert len(requests) == 1
    assert len(reviews) == 4
    assert not any("star reviews" in value for value in limits)


def test_strict_verdicts_accept_new_values_and_reject_legacy_ai_output():
    for value in [
        "worth_considering",
        "proceed_with_caution",
        "not_recommended",
        "insufficient_evidence",
    ]:
        assert judgment(verdict=value).verdict == value
    with pytest.raises(ValidationError):
        judgment(verdict="buy")


def test_response_can_read_legacy_cached_verdict_and_missing_metadata():
    data = {
        "status": "completed",
        "product": product().model_dump(),
        "verdict": "buy",
        "summary": "Legacy cached result",
        "reasons": [],
        "reviews": [],
        "coverage": {"reviews_analyzed": 0, "limitations": []},
    }
    restored = AnalysisResponse.model_validate(data)
    assert restored.verdict == "worth_considering"
    assert restored.positive_findings == []
    assert restored.sample_statistics.negative_count == 0


def test_quotes_citations_and_recurring_issues_require_real_source_support():
    _, reviews = parse_page(FIXTURE, product())
    validate_evidence(judgment(), reviews)
    invalid = judgment()
    invalid.negative_findings[0].evidence[0].quote = "invented quote"
    with pytest.raises(ValueError, match="quote"):
        validate_evidence(invalid, reviews)
    invalid = judgment()
    invalid.negative_findings[0].review_ids.append("RNEG1")
    with pytest.raises(ValueError, match="references"):
        validate_evidence(invalid, reviews)
    invalid = judgment()
    invalid.recurring_issues[0].review_ids = ["RNEG1"]
    invalid.recurring_issues[0].evidence = invalid.recurring_issues[0].evidence[:1]
    with pytest.raises(ValueError, match="recurring"):
        validate_evidence(invalid, reviews)


def test_strong_negative_verdict_requires_recurring_product_evidence():
    _, reviews = parse_page(FIXTURE, product())
    invalid = judgment(verdict="not_recommended")
    invalid.recurring_issues[0].category = "delivery"
    with pytest.raises(ValueError, match="product evidence"):
        validate_evidence(invalid, reviews)


def test_fitting_analysis_input_preserves_collected_reviews_and_positive_comparison():
    source = product().url
    reviews = [
        Review(id=f"RN{i}", text=f"Failure {i} " + "x" * 1400, stars=1, source_url=source)
        for i in range(15)
    ] + [
        Review(id=f"RP{i}", text=f"Strength {i} " + "y" * 1400, stars=5, source_url=source)
        for i in range(10)
    ]
    config = settings(openai_max_input_tokens=22000)
    service = AnalysisService(config)
    selected = service.fit_review_sample(product(), reviews)
    assert 3 <= len(selected) < len(reviews)
    assert any(review.stars == 1 for review in selected)
    assert any(review.stars == 5 for review in selected)
    assert len(reviews) == 25
    prepare_request(config, product(), selected)


def test_analysis_returns_sections_computed_counts_and_single_paid_call():
    listing = product()
    _, reviews = parse_page(FIXTURE, listing)
    value = judgment()
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.openai.com"),
        json={"choices": [{"message": {"content": value.model_dump_json()}}]},
    )
    config = settings(openai_api_key="test")
    with (
        patch(
            "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews, []))
        ),
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)) as post,
    ):
        result = asyncio.run(AnalysisService(config).analyze(listing.url))
    assert result.verdict == "proceed_with_caution"
    assert result.coverage.reviews_analyzed == 4
    assert result.recurring_issues[0].mention_count == 2
    assert result.sample_statistics.negative_count == 2
    assert len(result.negative_findings) == 1
    assert post.await_count == 1
    assert result.reasons


def test_refined_api_serializes_findings_and_reuses_cached_result():
    listing = product()
    _, reviews = parse_page(FIXTURE, listing)
    model_response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.openai.com"),
        json={"choices": [{"message": {"content": judgment().model_dump_json()}}]},
    )
    app = create_app(settings(openai_api_key="test"))

    async def submit():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                first = await client.request("POST", "/api/v1/analysis", json={"url": listing.url})
                second = await client.request(
                    "POST", "/api/v1/analysis", json={"url": listing.url + "?th=1"}
                )
                return first, second

    with (
        patch(
            "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews, []))
        ) as collect,
        patch("httpx.AsyncClient.post", AsyncMock(return_value=model_response)) as model,
    ):
        first, second = asyncio.run(submit())
    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body["product"]["overall_rating"] == 3.4
    assert body["coverage"]["reviews_analyzed"] == 4
    assert body["recurring_issues"][0]["mention_count"] == 2
    assert body["negative_findings"][0]["evidence"][0]["quote"] == "Glue fails"
    assert second.json()["cached"] is True
    assert second.json()["usage"]["reserved_cost_usd"] == 0
    assert model.await_count == collect.await_count == 1
