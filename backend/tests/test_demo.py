import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from app.main import create_app
from app.schemas.analysis import AnalysisResponse, Judgment
from app.schemas.demo import DemoDataset
from app.services.demo import PRODUCT_ROOT, DemoAnalysisService, DemoCatalog
from app.services.usage import UsageStore
from tests.test_langchain import wire as wire
from tests.test_refinement import settings


def supported_judgment():
    return Judgment(
        verdict="proceed_with_caution",
        summary="Amazon's saved summary reports adhesive concerns; direct reviews are unavailable.",
        positive_findings=[],
        negative_findings=[],
        recurring_issues=[],
        reliability_findings=[],
        key_takeaway="Consider the stated limitations before buying.",
        amazon_findings=[
            {
                "text": "Adhesion is a concern in the saved Amazon summary.",
                "source": "customers_say",
                "topic": None,
                "sentiment": "negative",
                "quote": "adhesive quality is poor",
            }
        ],
    )


def test_verified_dataset_only_lists_real_products_and_explicit_counts():
    catalog = DemoCatalog()
    dataset = catalog.get("prd1")
    assert [c.id for c in catalog.categories()] == ["led", "headphone", "mobile", "laptop", "tv"]
    assert catalog.categories()[0].product_count == 2
    assert len(catalog.products("led")) == 2
    assert [p.id for p in catalog.products("headphone")] == ["prd2", "prd8"]
    headphones = catalog.get("prd2")
    assert headphones.product.asin == "B0FC2W8WJM"
    assert headphones.product.url == "https://www.amazon.in/dp/B0FC2W8WJM"
    assert headphones.source_files  # Original screenshot names remain as provenance, not bundled files.
    assert catalog.safe_path(headphones.image_file).is_file()
    assert catalog.products("headphone")[0].image_path == "/api/v1/demo/products/prd2/image"
    assert sum(bucket.percentage for bucket in headphones.product.rating_distribution) == 100
    assert not dataset.reviews and len(dataset.topic_excerpts) == 8
    assert dataset.product.rating_count == 374
    assert {b.stars: b.percentage for b in dataset.product.rating_distribution} == {
        5: 44,
        4: 12,
        3: 12,
        2: 6,
        1: 26,
    }
    adhesive = next(t for t in dataset.product.feedback_topics if t.name == "Adhesive")
    assert (adhesive.mention_count, adhesive.positive_count, adhesive.negative_count) == (20, 4, 16)
    assert dataset.source_files
    assert catalog.safe_path(dataset.image_file).is_file()
    assert catalog.products("led")[0].image_path == "/api/v1/demo/products/prd1/image"
    assert not list(PRODUCT_ROOT.rglob("Screenshot *.png"))
    with pytest.raises(ValueError):
        catalog.safe_path("../../.env")


def test_dataset_validation_rejects_invented_identity_and_inconsistent_counts():
    data = DemoCatalog().get("prd1").model_dump()
    data["product"]["feedback_topics"][0]["positive_count"] = 100
    with pytest.raises(ValidationError, match="counts disagree"):
        DemoDataset.model_validate(data)
    data = DemoCatalog().get("prd1").model_dump()
    data["product"]["asin"] = "B000000000"
    with pytest.raises(ValidationError, match="identity"):
        DemoDataset.model_validate(data)
    data = DemoCatalog().get("prd1").model_dump()
    data["product"]["feedback_topics"][0]["positive_count"] = None
    assert DemoDataset.model_validate(data).product.feedback_topics[0].positive_count is None


def test_demo_endpoints_do_not_scrape_and_cache_real_mocked_model_analysis(wire):
    calls, state = wire
    state["content"] = supported_judgment().model_dump_json()
    app = create_app(settings(openai_api_key="test"))

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get("/api/v1/demo/categories")).json()[0]["id"] == "led"
                assert (await client.get("/api/v1/demo/categories/led/products")).json()[0][
                    "id"
                ] == "prd1"
                assert (await client.get("/api/v1/demo/products/prd1")).json()["reviews"] == []
                image = await client.get("/api/v1/demo/products/prd1/image")
                assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
                assert (await client.get("/api/v1/demo/products/missing")).status_code == 404
                headphones = await client.get("/api/v1/demo/categories/headphone/products")
                assert headphones.status_code == 200 and headphones.json()[0]["id"] == "prd2"
                assert (await client.get("/api/v1/demo/products/prd2")).status_code == 200
                assert (await client.get("/api/v1/demo/products/prd2/image")).status_code == 200
                assert not calls  # Browsing/inspection cannot spend model credit.
                one = await client.post("/api/v1/demo/products/prd1/analysis")
                two = await client.post("/api/v1/demo/products/prd1/analysis")
                assert one.status_code == two.status_code == 200
                result = AnalysisResponse.model_validate(one.json())
                assert result.verdict == "proceed_with_caution" and result.amazon_findings
                assert result.coverage.reviews_analyzed == 0 and not result.reviews
                assert two.json()["cached"] and len(calls) == 1
                assert (
                    await client.post("/api/v1/demo/products/prd1/analysis?workflow=other")
                ).status_code == 422
                assert (
                    await client.post("/api/v1/demo/products/prd1/analysis?workflow=langchain")
                ).status_code == 404

    with patch(
        "app.services.analysis.collect_reviews", side_effect=AssertionError("Amazon called")
    ):
        asyncio.run(exercise())
    evidence = json.loads(json.loads(calls[0].content)["messages"][1]["content"])
    assert evidence["written_reviews"] == [] and len(evidence["amazon_feedback_topics"]) == 8


def test_dataset_version_content_and_workflow_have_independent_caches(wire):
    calls, state = wire
    state["content"] = supported_judgment().model_dump_json()
    dataset = DemoCatalog().get("prd1")
    config = settings(openai_api_key="test")
    store = UsageStore(config)

    async def run():
        http = DemoAnalysisService(config, store=store, dataset=dataset)
        assert (await http.analyze(dataset.product.url)).amazon_findings
        assert (await http.analyze(dataset.product.url)).cached
        chain = DemoAnalysisService(config, store=store, dataset=dataset, workflow="langchain")
        assert (await chain.analyze(dataset.product.url)).amazon_findings
        assert (await chain.analyze(dataset.product.url)).cached
        newer = dataset.model_copy(deep=True, update={"version": dataset.version + 1})
        changed = dataset.model_copy(deep=True)
        changed.product.price_text = "Updated screenshot price"
        assert (
            len(
                {
                    http.cache_key("unused"),
                    chain.cache_key("unused"),
                    DemoAnalysisService(config, store=store, dataset=newer).cache_key("unused"),
                    DemoAnalysisService(config, store=store, dataset=changed).cache_key("unused"),
                }
            )
            == 4
        )

    with patch(
        "app.services.analysis.collect_reviews", side_effect=AssertionError("Amazon called")
    ):
        asyncio.run(run())
    assert len(calls) == 2 and store.usage().paid_attempts_today == 2


def test_mobile_dataset_analysis_and_cache_without_live_calls(wire):
    calls, state = wire
    catalog = DemoCatalog()
    saved = catalog.get("prd3")
    assert [p.id for p in catalog.products("mobile")] == ["prd3", "prd9"]
    assert saved.product.title == "Samsung Galaxy A56 5G"
    assert saved.product.asin == "B0H1WY466V"
    assert saved.product.overall_rating == 4.4 and saved.product.rating_count == 1473
    assert {b.stars: b.percentage for b in saved.product.rating_distribution} == {
        5: 65, 4: 22, 3: 4, 2: 2, 1: 7,
    }
    assert not saved.reviews and len(saved.topic_excerpts) == 32
    assert len(saved.source_files) == 9 and len(saved.product.feedback_topics) == 8
    assert catalog.safe_path(saved.image_file).is_file()
    heat = next(t for t in saved.product.feedback_topics if t.name == "Heat")
    assert (heat.sentiment, heat.mention_count, heat.positive_count, heat.negative_count) == (
        "mixed", 26, 8, 18,
    )
    judgment = supported_judgment().model_copy(deep=True)
    judgment.amazon_findings = []
    mock = judgment.model_dump()
    mock["summary"] = "The supplied summary supports daily use, with reported heating concerns."
    mock["amazon_findings"] = [{
        "text": "Heating is a concern in the supplied topic feedback.",
        "source": "topic", "topic": "Heat", "sentiment": "negative", "quote": heat.text,
    }]
    state["content"] = json.dumps(mock)
    app = create_app(settings(openai_api_key="test"))

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                products = await client.get("/api/v1/demo/categories/mobile/products")
                assert products.status_code == 200 and products.json()[0]["id"] == "prd3"
                assert (await client.get("/api/v1/demo/products/prd3")).status_code == 200
                image = await client.get("/api/v1/demo/products/prd3/image")
                assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
                assert not calls
                first = await client.post("/api/v1/demo/products/prd3/analysis")
                second = await client.post("/api/v1/demo/products/prd3/analysis")
                assert first.status_code == second.status_code == 200
                report = AnalysisResponse.model_validate(first.json())
                assert report.product.asin == "B0H1WY466V" and not report.reviews
                assert report.amazon_findings[0].topic == "Heat"
                assert second.json()["cached"] and len(calls) == 1

    with patch(
        "app.services.analysis.collect_reviews", side_effect=AssertionError("Amazon called")
    ):
        asyncio.run(exercise())


def test_demo_spending_guard_and_bad_citations_do_not_retry(wire):
    calls, state = wire
    dataset = DemoCatalog().get("prd1")
    config = settings(openai_api_key="test", ai_daily_budget_usd=0)
    service = DemoAnalysisService(config, store=UsageStore(config), dataset=dataset)
    result = asyncio.run(service.analyze(dataset.product.url))
    assert not calls and "budget" in result.summary.lower()
    config = settings(openai_api_key="different-test-key")
    invalid = supported_judgment().model_dump()
    invalid["amazon_findings"][0]["quote"] = "invented screenshot words"
    state["content"] = json.dumps(invalid)
    service = DemoAnalysisService(config, store=UsageStore(config), dataset=dataset)
    result = asyncio.run(service.analyze(dataset.product.url))
    assert len(calls) == 1 and result.status == "insufficient_evidence"
    assert not result.amazon_findings and service.store.usage().paid_attempts_today == 1


@pytest.mark.parametrize("product_id,category,rating_count,positive", [
    ("prd4", "laptop", 427, 79),
    ("prd5", "laptop", 599, 82),
    ("prd6", "tv", 5585, 73),
    ("prd7", "led", 1554, 72),
    ("prd8", "headphone", 1738, 66),
    ("prd9", "mobile", 531, 65),
])
def test_new_saved_products_have_valid_evidence_and_cache_analysis(
    wire, product_id, category, rating_count, positive
):
    calls, state = wire
    catalog = DemoCatalog()
    dataset = catalog.get(product_id)
    assert dataset.category_id == category
    assert dataset.product.rating_count == rating_count
    percentages = {b.stars: b.percentage for b in dataset.product.rating_distribution}
    assert sum(percentages.values()) == 100
    assert percentages[4] + percentages[5] == positive
    assert len(dataset.product.title) <= 35
    assert not dataset.reviews and dataset.topic_excerpts
    assert catalog.safe_path(dataset.image_file).is_file()
    topic = next(t for t in dataset.product.feedback_topics if t.sentiment == "positive")
    mock = supported_judgment().model_dump()
    mock["summary"] = "The supplied topic summary supports this finding; other feedback remains relevant."
    mock["amazon_findings"] = [{
        "text": "Positive feedback is present in the supplied topic summary.",
        "source": "topic", "topic": topic.name, "sentiment": "positive", "quote": topic.text[:240],
    }]
    state["content"] = json.dumps(mock)
    app = create_app(settings(openai_api_key="test"))

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                listing = await client.get(f"/api/v1/demo/categories/{category}/products")
                assert listing.status_code == 200
                assert product_id in {p["id"] for p in listing.json()}
                evidence = await client.get(f"/api/v1/demo/products/{product_id}")
                assert evidence.status_code == 200
                image = await client.get(f"/api/v1/demo/products/{product_id}/image")
                assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
                assert not calls
                first = await client.post(f"/api/v1/demo/products/{product_id}/analysis")
                second = await client.post(f"/api/v1/demo/products/{product_id}/analysis")
                assert first.status_code == second.status_code == 200
                report = AnalysisResponse.model_validate(first.json())
                assert report.product.asin == dataset.product.asin
                assert report.amazon_findings[0].topic == topic.name
                assert second.json()["cached"] and len(calls) == 1

    with patch("app.services.analysis.collect_reviews", side_effect=AssertionError("Amazon called")):
        asyncio.run(exercise())
