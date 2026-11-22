import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.integrations.amazon import balanced_review_sample, collect_reviews, parse_page
from app.integrations.llm import prepare_request, validate_evidence
from app.schemas.analysis import Review
from app.services.review_evidence import sample_statistics
from tests.test_refinement import FIXTURE, judgment, product, settings


def reviews(stars, count=10):
    return [
        Review(
            id=f"R{stars}{i}",
            text=f"Review {stars} {i}",
            stars=stars,
            date_iso=f"2026-11-{i + 1:02}",
            source_url=product().url,
        )
        for i in range(count)
    ]


def test_777_caps_categories_deduplicates_and_keeps_latest():
    source = reviews(5) + reviews(4) + reviews(3) + reviews(2) + reviews(1)
    selected = balanced_review_sample(source + source, 100)
    stats = sample_statistics(selected)
    assert len(selected) == len({r.id for r in selected}) == 21
    assert stats.positive_count == stats.neutral_count == stats.negative_count == 7
    assert min(r.date_iso for r in selected if r.stars == 3) == "2026-11-04"
    assert all(r.date_iso >= "2026-11-07" for r in selected if r.stars in (1, 2, 4, 5))


@pytest.mark.parametrize("stars", [1, 3, 5])
def test_missing_categories_are_not_backfilled_or_claimed_absent(stars):
    selected = balanced_review_sample(reviews(stars), 21)
    assert len(selected) == 7
    assert all(r.stars == stars for r in selected)


def test_all_star_routes_include_recent_sort_and_neutral_even_when_listing_is_full(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=FIXTURE)

    with patch(
        "app.integrations.amazon.httpx.AsyncClient",
        side_effect=lambda **kw: original(**kw, transport=httpx.MockTransport(handler)),
    ):
        _, selected, limitations = asyncio.run(collect_reviews(product(), settings()))
    assert len(requests) == 7
    assert {r.url.params.get("filterByStar") for r in requests[1:6]} == {
        "one_star",
        "two_star",
        "three_star",
        "four_star",
        "five_star",
    }
    assert all(r.url.params.get("sortBy") == "recent" for r in requests[1:])
    assert len(selected) == 4
    assert any("0 of up to 7 neutral" in line for line in limitations)
    assert any("not representative" in line for line in limitations)


def test_listing_scoped_product_details_and_untrusted_image_hosts():
    listing = product()
    parse_page(
        """<span id="productTitle">Fixture product</span>
      <img id="landingImage" src="https://m.media-amazon.com/images/I/fixture.jpg">
      <div id="corePrice_feature_div"><span class="a-price">
      <span class="a-offscreen">₹699.00</span></span></div>
      <div id="productOverview_feature_div"><table>
      <tr><td>Power</td><td>5 V</td></tr></table></div>""",
        listing,
    )
    assert listing.price_text == "₹699.00"
    assert listing.image_url.endswith("fixture.jpg")
    assert listing.specifications == [{"label": "Power", "value": "5 V"}]
    parse_page('<img id="landingImage" src="https://evil.example/x.jpg">', listing)
    assert listing.image_url.endswith("fixture.jpg")


def test_reliability_findings_require_exact_source_evidence_and_prompt_is_single_model():
    _, source = parse_page(FIXTURE, product())
    value = judgment()
    value.reliability_findings = value.negative_findings[:]
    validate_evidence(value, source)
    value.reliability_findings[0].evidence[0].quote = "Invented durability"
    with pytest.raises(ValueError, match="quote"):
        validate_evidence(value, source)
    payload, _ = prepare_request(settings(), product(), source)
    assert payload["model"] == "gpt-5-nano"
    assert "balanced 7 positive" in payload["messages"][0]["content"]
    assert "reliability_findings" in payload["response_format"]["json_schema"]["schema"]["required"]
