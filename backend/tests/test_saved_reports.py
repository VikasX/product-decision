import asyncio
import shutil
from unittest.mock import patch

import httpx
import pytest

from app.api.routes import demo
from app.main import create_app
from app.services.demo import PRODUCT_ROOT, DemoCatalog
from app.services.saved_reports import SavedReport, SavedReportCatalog, validate_report
from tests.test_refinement import settings


def test_saved_reports_validate_without_removing_products_from_the_catalog():
    catalog = SavedReportCatalog()
    ready = catalog.datasets()
    assert {d.id for _, d in ready} == {f"prd{i}" for i in range(1, 10)}
    assert sum(c.product_count for c in DemoCatalog().categories()) == 9
    for _, dataset in ready:
        report = catalog.report_for(dataset)
        assert report.status in {"completed", "insufficient_evidence"} and report.usage is None
        assert report.product == dataset.product and report.cached


def test_stale_missing_or_fabricated_reports_are_never_published(tmp_path):
    root = tmp_path / "product"
    shutil.copytree(PRODUCT_ROOT, root)
    catalog = SavedReportCatalog(root)
    dataset = DemoCatalog(root).get("prd9")
    path = catalog.report_path("prd9")
    saved = SavedReport.model_validate_json(path.read_text(encoding="utf-8"))
    changed = dataset.model_copy(deep=True)
    changed.product.price_text = "changed evidence"
    with pytest.raises(ValueError, match="current dataset"):
        validate_report(saved, changed)
    saved.report.amazon_findings[0].quote = "FABRICATED REVIEW WORDS"
    with pytest.raises(ValueError, match="unverified"):
        validate_report(saved, dataset)
    path.write_text(saved.model_dump_json(), encoding="utf-8")
    assert "prd9" not in {d.id for _, d in catalog.datasets()}
    path.unlink()
    assert "prd9" not in {d.id for _, d in catalog.datasets()}
    path.write_text('{"broken":true}', encoding="utf-8")
    assert "prd9" not in {d.id for _, d in catalog.datasets()}


@pytest.mark.parametrize("overrides", [
    {"public_saved_reports_only": True},
    {"public_saved_reports_only": False, "vercel": True},
])
def test_public_requests_never_create_llm_services_or_use_spending_storage(overrides, tmp_path, monkeypatch):
    # Simulate a missing report in an isolated copy even after all real products are published.
    root = tmp_path / "product"
    shutil.copytree(PRODUCT_ROOT, root)
    reports = SavedReportCatalog(root)
    reports.report_path("prd5").unlink(missing_ok=True)
    monkeypatch.setattr(demo, "published", reports)
    app = create_app(settings(**overrides))

    async def exercise():
        async with app.router.lifespan_context(app):
            assert not hasattr(app.state, "analysis_service")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                categories = (await client.get("/api/v1/demo/categories")).json()
                assert {c["id"] for c in categories} == {"led", "mobile", "headphone", "laptop", "tv"}
                assert sum(c["product_count"] for c in categories) == 9
                for category in categories:
                    products = (await client.get(f'/api/v1/demo/categories/{category["id"]}/products')).json()
                    for product in products:
                        endpoint = f'/api/v1/demo/products/{product["id"]}/analysis'
                        first = await client.get(endpoint)
                        second = await client.get(endpoint)
                        legacy = await client.post(endpoint + "?workflow=langchain")
                        expected = 200 if product["analysis_ready"] else 404
                        assert first.status_code == second.status_code == legacy.status_code == expected
                        assert first.json() == second.json() == legacy.json()
                        if product["analysis_ready"]:
                            assert first.json()["usage"] is None
                # Even known evidence without a ready report cannot become an AI cache miss.
                assert (await client.get("/api/v1/demo/products/prd5/analysis")).status_code == 404
                assert (await client.post("/api/v1/demo/products/prd5/analysis")).status_code == 404
                assert (await client.get("/api/v1/demo/products/unknown/analysis")).status_code == 404
                for endpoint in ("/api/v1/analysis", "/api/v1/analysis/langchain"):
                    response = await client.post(endpoint, json={"url": "https://www.amazon.in/dp/B0D5Q7RWF6"})
                    assert response.status_code == 404

    with patch("app.main.AnalysisService", side_effect=AssertionError("LLM service created")), \
         patch("app.services.analysis.collect_reviews", side_effect=AssertionError("Amazon called")), \
         patch("app.services.analysis.analyze_reviews", side_effect=AssertionError("OpenAI called")):
        asyncio.run(exercise())


def test_private_generation_rejects_remote_clients_even_with_spoofed_headers():
    app = create_app(settings())

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("203.0.113.1", 1234))
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
                response = await client.post("/api/v1/analysis", json={"url": "https://www.amazon.in/dp/B0D5Q7RWF6"}, headers={"x-forwarded-for": "127.0.0.1", "origin": "http://localhost:3000"})
                assert response.status_code == 403
                response = await client.post("/api/v1/demo/products/prd1/analysis")
                assert response.status_code == 403

    asyncio.run(exercise())
