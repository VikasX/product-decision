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
    assert [c.id for c in catalog.categories()] == ["led"]
    assert catalog.categories()[0].product_count == 1
    assert len(catalog.products("led")) == 1
    assert catalog.products("headphone") == []
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
    assert all((PRODUCT_ROOT / source).is_file() for source in dataset.source_files)
    assert catalog.safe_path(dataset.image_file).is_file()
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
                assert image.status_code == 200 and image.headers["content-type"] == "image/png"
                assert (await client.get("/api/v1/demo/products/missing")).status_code == 404
                assert (
                    await client.get("/api/v1/demo/categories/headphone/products")
                ).status_code == 404
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
        newer = dataset.model_copy(deep=True, update={"version": 2})
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
