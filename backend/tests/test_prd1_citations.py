import asyncio
import copy
import json

import httpx
import pytest

from app.integrations.llm import prepare_request, validate_evidence
from app.main import create_app
from app.schemas.analysis import Judgment
from app.services.demo import DemoCatalog
from tests.test_demo import supported_judgment
from tests.test_langchain import wire as wire
from tests.test_refinement import settings


def test_prd1_prompt_citations_match_validator_and_reject_mismatched_sources():
    dataset = DemoCatalog().get("prd1")
    config = settings()
    payload, _ = prepare_request(config, dataset.product, dataset.reviews)
    instructions = payload["messages"][0]["content"]
    evidence = json.loads(payload["messages"][1]["content"])
    citations = evidence["allowed_amazon_citations"]
    assert "copy source, topic and quote together" in instructions
    assert (
        len(payload["messages"][1]["content"].encode("utf-8")) <= config.analysis_evidence_max_bytes
    )
    assert len(citations) == 9 and evidence["written_reviews"] == []
    schema = payload["response_format"]["json_schema"]["schema"]
    for field in (
        "positive_findings",
        "negative_findings",
        "reliability_findings",
        "recurring_issues",
    ):
        assert schema["properties"][field]["maxItems"] == 0
    choices = schema["properties"]["amazon_findings"]["items"]["anyOf"]
    assert [
        {field: choice["properties"][field]["enum"][0] for field in ("source", "topic", "quote")}
        for choice in choices
    ] == citations
    assert all(choice["additionalProperties"] is False for choice in choices)
    assert citations[0]["source"] == "customers_say" and citations[0]["topic"] is None
    topics = {topic.name: topic for topic in dataset.product.feedback_topics}
    assert {c["topic"] for c in citations[1:]} == set(topics)

    def judgment(citation):
        topic = topics.get(citation["topic"])
        return Judgment(
            verdict="proceed_with_caution",
            summary="Saved secondary evidence only.",
            positive_findings=[],
            negative_findings=[],
            reliability_findings=[],
            recurring_issues=[],
            key_takeaway="Consider the supplied evidence limitations.",
            amazon_findings=[
                {
                    **citation,
                    "text": "Finding supported by the supplied source.",
                    "sentiment": topic.sentiment if topic else "mixed",
                }
            ],
        )

    # Every copyable triple uses the existing schema and exact-source validator unchanged.
    for citation in citations:
        source = (
            dataset.product.customers_say
            if citation["topic"] is None
            else topics[citation["topic"]].text
        )
        assert citation["quote"] in source and 0 < len(citation["quote"]) <= 240
        validate_evidence(judgment(citation), [], dataset.product)

    adhesive = next(c for c in citations if c["topic"] == "Adhesive")
    invalid = []
    renamed = copy.deepcopy(adhesive)
    renamed["topic"] = "adhesive"  # Similar label is still not the exact saved identifier.
    invalid.append(renamed)
    wrong_source = copy.deepcopy(adhesive)
    wrong_source.update(source="customers_say", topic=None)
    invalid.append(wrong_source)
    wrong_null = copy.deepcopy(citations[0])
    wrong_null["topic"] = "Quality"
    invalid.append(wrong_null)
    fabricated = copy.deepcopy(adhesive)
    fabricated["quote"] = "This product caught fire yesterday."
    invalid.append(fabricated)
    for citation in invalid:
        with pytest.raises(ValueError, match="unverified Amazon source evidence"):
            validate_evidence(judgment(citation), [], dataset.product)


@pytest.mark.parametrize("workflow", ["httpx", "langchain"])
def test_prd1_endpoint_uses_evidence_bound_schema_and_caches_success(wire, workflow):
    calls, state = wire
    dataset = DemoCatalog().get("prd1")
    config = settings(openai_api_key="test-only", langchain_lab_enabled=True)
    prepared, _ = prepare_request(config, dataset.product, [])
    citation = next(
        c
        for c in json.loads(prepared["messages"][1]["content"])["allowed_amazon_citations"]
        if c["topic"] == "Adhesive"
    )
    judgment = supported_judgment().model_dump()
    judgment["amazon_findings"][0].update(citation)
    state["content"] = json.dumps(judgment)
    app = create_app(config)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                base_url="http://test",
            ) as client:
                url = f"/api/v1/demo/products/prd1/analysis?workflow={workflow}"
                first = await client.post(url)
                second = await client.post(url)
                assert first.status_code == second.status_code == 200
                assert first.json()["reviews"] == []
                assert first.json()["amazon_findings"][0]["quote"] == citation["quote"]
                assert second.json()["cached"] is True

    asyncio.run(run())
    assert len(calls) == 1
    sent_schema = json.loads(calls[0].content)["response_format"]["json_schema"]["schema"]
    for field in (
        "positive_findings",
        "negative_findings",
        "reliability_findings",
        "recurring_issues",
    ):
        assert sent_schema["properties"][field]["maxItems"] == 0
    assert len(sent_schema["properties"]["amazon_findings"]["items"]["anyOf"]) == 9
