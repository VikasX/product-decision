import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.amazon import collect_reviews, parse_page
from app.integrations.llm import prepare_request, validate_evidence
from app.schemas.analysis import Judgment, Product, RatingBucket, Review
from app.services.analysis import AnalysisService
from app.services.review_evidence import compact_evidence, select_page_reviews
from app.services.usage import CostProtectionError
from tests.test_refinement import product, settings

HTML = (Path(__file__).parent / "fixtures/amazon_summary.html").read_text(encoding="utf-8")


def data():
    listing = product()
    _, reviews = parse_page(HTML, listing)
    return listing, reviews


def summary_judgment():
    return Judgment(
        verdict="proceed_with_caution",
        summary="Amazon reports adhesive concerns.",
        positive_findings=[],
        negative_findings=[],
        reliability_findings=[],
        recurring_issues=[],
        key_takeaway="Check adhesion requirements; direct evidence is limited.",
        amazon_findings=[
            {
                "text": "Amazon reports adhesive concerns.",
                "sentiment": "negative",
                "source": "customers_say",
                "topic": None,
                "quote": "weak adhesive",
            }
        ],
    )


def test_extracts_summary_histogram_topics_without_inventing_partial_counts():
    listing, reviews = data()
    assert listing.rating_count == 374 and len(listing.rating_distribution) == 5
    assert "weak adhesive" in listing.customers_say
    topics = {t.name: t for t in listing.feedback_topics}
    assert topics["Appearance"].sentiment == "positive"
    assert topics["Adhesive"].sentiment == "negative"
    assert topics["Connectivity"].sentiment == "mixed"
    assert topics["Appearance"].mention_count == 23
    assert topics["Appearance"].positive_count == 20
    assert topics["Connectivity"].mention_count is None and topics["Quality"].sentiment is None
    assert reviews[0].date_iso == "2026-11-15"


@pytest.mark.parametrize("html", [HTML, '<input id="captchacharacters">'])
def test_collection_requests_only_listing_and_never_filtered_routes(html):
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=html)

    with patch(
        "app.integrations.amazon.httpx.AsyncClient",
        side_effect=lambda **kw: original(**kw, transport=httpx.MockTransport(handler)),
    ):
        listing, reviews, limitations = asyncio.run(collect_reviews(product(), settings()))
    assert len(requests) == 1 and requests[0].url.path.startswith("/dp/")
    assert not any("star reviews" in line for line in limitations)
    if "captchacharacters" in html:
        assert not reviews and listing.customers_say is None
        assert any("CAPTCHA" in line for line in limitations)


def test_missing_summary_and_unrelated_text_are_not_guessed():
    listing = product()
    parse_page("<div>Customers say this recommendation is amazing</div>", listing)
    assert listing.customers_say is None and listing.feedback_topics == []


def test_new_product_widget_summary_aspect_labels_and_review_title():
    # Reduced markup matching the public FLYNGO product widget, not a live API test.
    html = """<div data-testid="overall-summary">Unrelated recommendation</div>
    <div id="customerReviews">
      <div data-testid="overall-summary">Customers appreciate the colors but report
      mixed reliability and app connectivity.</div>
      <div data-testid="aspect-list">
        <div role="tab" aria-label="Positive aspect, quality, 79 mentions">Quality(79)</div>
        <div role="tab" aria-label="Mixed aspect, reliability, 34 mentions">Reliability(34)</div>
        <div role="tab">No usable sentiment</div>
      </div>
      <div data-hook="review" id="RTEST123">
        <h5 data-hook="reviewTitle">Problems with mobile app connection</h5>
        <i data-hook="review-star-rating">1 out of 5 stars</i>
        <div data-hook="reviewRichContentContainer"><p>The app does not connect.</p></div>
      </div>
    </div>"""
    listing = product()
    _, reviews = parse_page(html, listing)
    assert listing.customers_say.startswith("Customers appreciate")
    assert len(listing.feedback_topics) == 2
    reliability, quality = listing.feedback_topics
    assert reliability.name == "reliability" and reliability.sentiment == "mixed"
    assert reliability.mention_count == 34 and reliability.negative_count is None
    assert quality.sentiment == "positive" and quality.mention_count == 79
    assert reviews[0].title == "Problems with mobile app connection"
    assert reviews[0].stars == 1 and reviews[0].text == "The app does not connect."


def test_partial_listing_reports_server_collection_failure_without_paid_call():
    html = '<span id="productTitle">Partial product listing</span>'
    listing = product()
    original = httpx.AsyncClient
    with patch(
        "app.integrations.amazon.httpx.AsyncClient",
        side_effect=lambda **kw: original(
            **kw, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
        ),
    ):
        collected = asyncio.run(collect_reviews(listing, settings()))
    assert any("retrieved HTML" in line for line in collected[2])
    with (
        patch("app.services.analysis.collect_reviews", AsyncMock(return_value=collected)),
        patch("httpx.AsyncClient.post", AsyncMock()) as post,
    ):
        result = asyncio.run(AnalysisService(settings(openai_api_key="test")).analyze(listing.url))
    assert "unavailable to the server" in result.summary
    assert "No paid AI analysis" in result.summary
    post.assert_not_awaited()


def test_compact_input_bounds_reviews_and_keeps_negative_evidence():
    listing, _ = data()
    reviews = [
        Review(id=f"RP{i}", stars=5, text=f"Positive {i} " + "x" * 2000, source_url=listing.url)
        for i in range(12)
    ]
    negative = Review(id="RNEG", stars=1, text="Adhesive failed", source_url=listing.url)
    payload = compact_evidence(listing, reviews + [negative, negative], 12000)
    assert len(payload["written_reviews"]) == 8
    assert payload["written_reviews"][0]["id"] == "RNEG"
    assert all(len(r["text"]) <= 1000 for r in payload["written_reviews"])
    assert "url" not in payload and "specifications" not in payload
    with pytest.raises(CostProtectionError):
        compact_evidence(listing, reviews, 2000)
    assert len(select_page_reviews(reviews, 5)) == 5


@pytest.mark.parametrize("review_count", [0, 1])
def test_summary_and_ratings_support_one_guarded_call_without_three_reviews(review_count):
    listing, reviews = data()
    reviews = reviews[:review_count]
    config = settings(openai_api_key="test")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.openai.com"),
        json={"choices": [{"message": {"content": summary_judgment().model_dump_json()}}]},
    )
    with (
        patch(
            "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews, []))
        ),
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response)) as post,
    ):
        service = AnalysisService(config)
        result = asyncio.run(service.analyze(listing.url))
        cached = asyncio.run(service.analyze(listing.url))
    assert (
        result.verdict == "proceed_with_caution"
        and result.coverage.reviews_analyzed == review_count
    )
    assert result.amazon_findings and not result.reasons and not result.negative_findings
    assert cached.cached and post.await_count == 1
    wire = post.call_args.kwargs["json"]
    assert json.loads(wire["messages"][1]["content"])["amazon_ai_summary"] == listing.customers_say


def test_amazon_sources_cannot_invent_quotes_topics_or_review_ids():
    listing, reviews = data()
    valid = summary_judgment()
    validate_evidence(valid, reviews, listing)
    valid.amazon_findings[0].quote = "invented source text"
    with pytest.raises(ValueError, match="Amazon"):
        validate_evidence(valid, reviews, listing)
    valid = summary_judgment()
    valid.amazon_findings[0].quote = " "
    with pytest.raises(ValueError, match="Amazon"):
        validate_evidence(valid, reviews, listing)
    valid = summary_judgment()
    valid.amazon_findings[0].source = "topic"
    valid.amazon_findings[0].topic = "Imaginary topic"
    with pytest.raises(ValueError, match="Amazon"):
        validate_evidence(valid, reviews, listing)


def test_ratings_alone_do_not_trigger_spending():
    listing = Product(
        asin=product().asin,
        title="Fixture",
        url=product().url,
        rating_distribution=[RatingBucket(stars=s, percentage=20) for s in range(1, 6)],
    )
    with (
        patch("app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, [], []))),
        patch("httpx.AsyncClient.post", AsyncMock()) as post,
    ):
        result = asyncio.run(AnalysisService(settings(openai_api_key="test")).analyze(listing.url))
    assert result.status == "insufficient_evidence"
    post.assert_not_awaited()


def test_both_preparers_share_the_same_compact_evidence():
    from app.integrations.langchain_llm import prepare_langchain_request

    listing, reviews = data()
    http, _ = prepare_request(settings(), listing, reviews)
    chain, _ = prepare_langchain_request(settings(), listing, reviews)
    assert http["messages"] == chain["messages"]
