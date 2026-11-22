import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.integrations.llm import ModelOutputError, parse_http_judgment, validate_evidence
from app.main import create_app
from app.services.demo import DemoAnalysisService, DemoCatalog
from app.services.usage import UsageStore
from tests.test_demo import supported_judgment
from tests.test_langchain import wire as wire
from tests.test_refinement import settings


def test_mixed_topic_supports_specific_concern_without_changing_source_sentiment():
    dataset = DemoCatalog().get("prd1")
    judgment = supported_judgment()
    finding = judgment.amazon_findings[0]
    finding.source = "topic"
    finding.topic = "Quality"
    finding.quote = "receiving faulty products"
    assert (
        next(t for t in dataset.product.feedback_topics if t.name == "Quality").sentiment == "mixed"
    )
    validate_evidence(judgment, [], dataset.product)
    # A fabricated quote or a claim opposing an explicit single-sentiment topic still fails.
    finding.quote = "invented words"
    with pytest.raises(ValueError, match="unverified Amazon"):
        validate_evidence(judgment, [], dataset.product)
    finding.topic = "Appearance"
    finding.quote = "premium look"
    with pytest.raises(ValueError, match="sentiment"):
        validate_evidence(judgment, [], dataset.product)


@pytest.mark.parametrize(
    "finish,refusal,code",
    [
        ("length", None, "output_truncated"),
        ("stop", "PRIVATE REFUSAL BODY", "output_refused"),
    ],
)
def test_incomplete_or_refused_response_is_not_parsed_as_product_evidence(
    finish, refusal, code, caplog
):
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {"content": "PRIVATE SOURCE TEXT", "refusal": refusal},
                }
            ]
        },
    )
    with caplog.at_level("INFO"), pytest.raises(ModelOutputError) as failure:
        parse_http_judgment(response)
    assert failure.value.code == code
    assert "http_status=200" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_schema_logs_field_and_type_without_model_text_or_extra_field_names(caplog):
    data = supported_judgment().model_dump()
    data["verdict"] = "PRIVATE INPUT TEXT"
    data["PRIVATE EXTRA KEY"] = "PRIVATE VALUE"
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(data)},
                }
            ]
        },
    )
    with pytest.raises(ModelOutputError, match="schema"):
        parse_http_judgment(response)
    assert "ValidationError" in caplog.text and "verdict" in caplog.text
    assert "literal_error" in caplog.text and "PRIVATE" not in caplog.text


def test_demo_technical_error_is_502_and_cached_without_second_paid_attempt(wire):
    calls, state = wire
    data = supported_judgment().model_dump()
    data["amazon_findings"][0]["quote"] = "fabricated source quote"
    state["content"] = json.dumps(data)
    app = create_app(settings(openai_api_key="test"))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for _ in range(2):
                    response = await client.post("/api/v1/demo/products/prd1/analysis")
                    assert response.status_code == 502
                    assert "detail" in response.json() and "verdict" not in response.json()
                assert app.state.analysis_service.store.usage().paid_attempts_today == 1

    with patch(
        "app.services.analysis.collect_reviews", side_effect=AssertionError("Amazon called")
    ):
        asyncio.run(run())
    assert len(calls) == 1


def test_genuine_model_insufficient_evidence_remains_a_200_report(wire):
    calls, state = wire
    judgment = supported_judgment()
    judgment.verdict = "insufficient_evidence"
    judgment.amazon_findings = []
    judgment.summary = "The supplied sources do not support a buying recommendation."
    state["content"] = judgment.model_dump_json()
    app = create_app(settings(openai_api_key="test"))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/demo/products/prd1/analysis")
                assert response.status_code == 200
                assert response.json()["verdict"] == "insufficient_evidence"

    asyncio.run(run())
    assert len(calls) == 1


def test_zero_review_prompt_is_explicit_and_timeout_keeps_reservation():
    dataset = DemoCatalog().get("prd1")
    config = settings(openai_api_key="test")
    service = DemoAnalysisService(config, store=UsageStore(config), dataset=dataset)
    payload, _ = service.prepare_llm_request(dataset.product, [])
    assert "Return empty positive_findings" in payload["messages"][0]["content"]
    original = httpx.AsyncClient
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE NETWORK ERROR", request=request)

    with patch(
        "app.integrations.llm.httpx.AsyncClient",
        side_effect=lambda **kw: original(**kw, transport=httpx.MockTransport(timeout)),
    ):
        result = asyncio.run(service.analyze(dataset.product.url))
    assert any("request_timeout" in line for line in result.coverage.limitations)
    assert len(calls) == 1 and service.store.usage().paid_attempts_today == 1
