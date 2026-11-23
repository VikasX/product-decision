import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.integrations.llm import prepare_request, validate_evidence
from app.schemas.analysis import Judgment
from app.services.demo import DemoAnalysisService, DemoCatalog
from app.services.usage import UsageStore
from tests.test_langchain import wire as wire
from tests.test_refinement import settings


def test_bad_request_logs_only_allowlisted_diagnostics_without_private_provider_text(caplog):
    dataset = DemoCatalog().get("prd5")
    service = DemoAnalysisService(settings(openai_api_key="test"), store=UsageStore(settings()), dataset=dataset)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {
        "code": "invalid_json_schema", "param": "response_format",
        "message": "PRIVATE CUSTOMER DATA AND SECRET KEY",
    }})
    failure = httpx.HTTPStatusError("PRIVATE HEADER", request=request, response=response)
    with patch.object(service, "judge_reviews", side_effect=failure):
        report, verified = asyncio.run(service.run_analysis(dataset.product.url, dataset.product.asin))
    assert not verified and report.status == "insufficient_evidence"
    assert "provider_code=invalid_json_schema parameter=response_format" in caplog.text
    assert "PRIVATE" not in caplog.text and "SECRET" not in caplog.text


def test_primebook_quoted_source_avoids_invalid_schema_literals_and_keeps_strict_validation(wire):
    calls, state = wire
    dataset = DemoCatalog().get("prd5")
    config = settings(openai_api_key="test")
    payload, _ = prepare_request(config, dataset.product, [])
    schema = payload["response_format"]["json_schema"]["schema"]
    choices = schema["properties"]["amazon_findings"]["items"]["anyOf"]
    # This is the real Primebook input that triggered OpenAI's HTTP 400.
    quoted = [t for t in dataset.product.feedback_topics if t.text and '"' in t.text[:240]]
    assert quoted
    for topic_with_quotes in quoted:
        branch = next(c for c in choices if c["properties"]["topic"].get("enum") == [topic_with_quotes.name])
        assert "enum" not in branch["properties"]["quote"]
        assert branch["properties"]["quote"]["maxLength"] == 240
    # No enum in the outgoing schema may reintroduce the provider-rejected literal.
    def check_enums(node):
        if isinstance(node, dict):
            assert all(not isinstance(v, str) or '"' not in v for v in node.get("enum", []))
            for value in node.values():
                check_enums(value)
        elif isinstance(node, list):
            for value in node:
                check_enums(value)
    check_enums(schema)
    topic = dataset.product.feedback_topics[0]
    judgment = Judgment(verdict="worth_considering", summary="Saved feedback supports everyday student use.",
                        positive_findings=[], negative_findings=[], recurring_issues=[], reliability_findings=[],
                        key_takeaway="Consider the supplied performance and battery feedback.",
                        amazon_findings=[{"text": "Quality feedback is positive.", "source": "topic",
                                          "topic": topic.name, "quote": topic.text[:240], "sentiment": "positive"}])
    state["content"] = judgment.model_dump_json()
    service = DemoAnalysisService(config, store=UsageStore(config), dataset=dataset)
    result, verified = asyncio.run(service.run_analysis(dataset.product.url, dataset.product.asin))
    assert verified and result.status == "completed" and len(calls) == 1
    judgment.amazon_findings[0].quote = "invented unsupported statement"
    with pytest.raises(ValueError, match="unverified"):
        validate_evidence(judgment, [], dataset.product)
    # Real wording attached to an unrelated source also fails after ordinary schema validation.
    judgment.amazon_findings[0].quote = topic.text[:240]
    judgment.amazon_findings[0].source = "customers_say"
    judgment.amazon_findings[0].topic = None
    with pytest.raises(ValueError, match="unverified"):
        validate_evidence(judgment, [], dataset.product)
