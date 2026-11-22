import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.langchain_llm import analyze_reviews_langchain, prepare_langchain_request
from app.integrations.llm import AttemptCharge, analyze_reviews
from app.main import create_app
from app.schemas.analysis import AnalysisResponse
from app.services.analysis import AnalysisService
from app.services.langchain_analysis import LangChainAnalysisService
from app.services.mongo_usage import MongoUsageStore
from app.services.usage import PROMPT_VERSION, CostProtectionError, UsageStore
from tests.test_refinement import FIXTURE, judgment, parse_page, product, settings


@pytest.fixture
def wire(monkeypatch):
    """Run real LangChain + SDK serialization/parsing, intercepting every HTTP transport."""
    requests = []
    state = {"status": 200, "content": judgment().model_dump_json()}

    def handle(request):
        requests.append(request)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": {"message": "private error"}})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5-nano",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": state["content"]},
                    }
                ],
            },
        )

    original_init = httpx.AsyncClient.__init__

    def init(client, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handle))
        original_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **kw: pytest.fail("Unexpected sync IO"))
    return requests, state


def evidence():
    listing = product()
    _, reviews = parse_page(FIXTURE, listing)
    return listing, reviews


def test_real_langchain_chain_returns_pydantic_and_sends_one_bounded_nano_request(wire):
    requests, _ = wire
    listing, reviews = evidence()
    config = settings(openai_api_key="test")
    charge = AttemptCharge()
    result = asyncio.run(
        analyze_reviews_langchain(config, listing, reviews, charge, UsageStore(config))
    )
    assert result == judgment()
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["model"] == "gpt-5-nano"
    assert payload["reasoning_effort"] == "minimal"
    assert payload["max_completion_tokens"] == 2000
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in payload
    assert len(payload["messages"]) == 2
    assert "balanced 7 positive" in payload["messages"][0]["content"]
    assert json.loads(payload["messages"][1]["content"])["reviews"][0]["id"] == reviews[0].id
    actual_bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 8192
    assert charge.reserved >= actual_bound * 50 + 2000 * 400
    assert UsageStore(config).usage().paid_attempts_today == 1


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_provider_failure_never_retries_and_keeps_charge(wire, status):
    requests, state = wire
    state["status"] = status
    listing, reviews = evidence()
    config = settings(openai_api_key="test")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            analyze_reviews_langchain(config, listing, reviews, AttemptCharge(), UsageStore(config))
        )
    assert len(requests) == 1
    assert UsageStore(config).usage().paid_attempts_today == 1
    if status == 401:
        with pytest.raises(CostProtectionError, match="rejected recently"):
            asyncio.run(analyze_reviews(config, listing, reviews))
        assert len(requests) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"ai_daily_budget_usd": 0},
        {"ai_lifetime_budget_usd": 0},
        {"ai_max_paid_attempts_per_day": 0},
        {"openai_max_input_tokens": 1000},
    ],
)
def test_langchain_limits_reject_before_network(wire, override):
    requests, _ = wire
    listing, reviews = evidence()
    config = settings(openai_api_key="test", **override)
    with pytest.raises(CostProtectionError):
        asyncio.run(
            analyze_reviews_langchain(config, listing, reviews, AttemptCharge(), UsageStore(config))
        )
    assert not requests
    assert UsageStore(config).usage().paid_attempts_today == 0


@pytest.mark.parametrize("invalid", ["not json", "invented quote"])
def test_invalid_langchain_output_never_repairs_or_retries(wire, invalid):
    requests, state = wire
    if invalid == "not json":
        state["content"] = invalid
    else:
        value = judgment()
        value.negative_findings[0].evidence[0].quote = invalid
        state["content"] = value.model_dump_json()
    listing, reviews = evidence()
    config = settings(openai_api_key="test")
    with pytest.raises(ValueError):
        asyncio.run(
            analyze_reviews_langchain(config, listing, reviews, AttemptCharge(), UsageStore(config))
        )
    assert len(requests) == 1
    assert UsageStore(config).usage().paid_attempts_today == 1


def test_endpoints_keep_separate_judgments_and_cache_but_reuse_http_review_sample(wire):
    requests, _ = wire
    listing, reviews = evidence()
    config = settings(openai_api_key="test", langchain_lab_enabled=True)
    app = create_app(config)

    async def run():
        async with app.router.lifespan_context(app):
            assert app.state.analysis_service.store is app.state.langchain_analysis_service.store
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                base_url="http://test",
            ) as client:
                first = await client.post("/api/v1/analysis", json={"url": listing.url})
                lab = await client.post("/api/v1/analysis/langchain", json={"url": listing.url})
                cached = await client.post("/api/v1/analysis/langchain", json={"url": listing.url})
                return first, lab, cached

    with patch(
        "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews, []))
    ) as collect:
        first, lab, cached = asyncio.run(run())
    assert first.status_code == lab.status_code == cached.status_code == 200
    assert set(first.json()) == set(lab.json())
    assert not first.json()["cached"] and not lab.json()["cached"]
    assert cached.json()["cached"] and cached.json()["usage"]["reserved_cost_usd"] == 0
    assert "independent judgment" in " ".join(lab.json()["coverage"]["limitations"])
    AnalysisResponse.model_validate(lab.json())
    assert len(requests) == 2 and collect.await_count == 1
    http_key = f"amazon.in:{listing.asin}:{config.openai_model}:{PROMPT_VERSION}"
    lab_key = http_key + LangChainAnalysisService.cache_namespace
    assert MongoUsageStore.state_id(http_key) != MongoUsageStore.state_id(lab_key)
    assert UsageStore(config).cached(http_key) is not None
    assert UsageStore(config).cached(lab_key) is not None
    assert UsageStore(config).usage().paid_attempts_today == 2


def test_lab_without_http_cache_collects_once_and_reuses_its_own_result(wire):
    requests, _ = wire
    listing, reviews = evidence()
    config = settings(openai_api_key="test")
    service = LangChainAnalysisService(config)
    with patch(
        "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews, []))
    ) as collect:
        first = asyncio.run(service.analyze(listing.url))
        cached = asyncio.run(service.analyze(listing.url))
    assert first.verdict == "proceed_with_caution" and cached.cached
    assert collect.await_count == len(requests) == 1
    assert len(first.reviews) == first.coverage.reviews_analyzed == 4
    assert prepare_langchain_request(config, listing, reviews)[1] > 0


def test_http_and_langchain_share_one_daily_attempt_cap(wire):
    requests, _ = wire
    listing, reviews = evidence()
    config = settings(openai_api_key="test", ai_max_paid_attempts_per_day=1)
    with patch(
        "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews, []))
    ) as collect:
        first = asyncio.run(AnalysisService(config).analyze(listing.url))
        lab = asyncio.run(LangChainAnalysisService(config).analyze(listing.url))
    assert first.status == "completed"
    assert lab.status == "insufficient_evidence"
    assert "No new AI request was made." in lab.coverage.limitations
    assert len(requests) == collect.await_count == lab.usage.paid_attempts_today == 1


def test_insufficient_reviews_do_not_call_langchain(wire):
    requests, _ = wire
    listing, reviews = evidence()
    service = LangChainAnalysisService(settings(openai_api_key="test"))
    with patch(
        "app.services.analysis.collect_reviews", AsyncMock(return_value=(listing, reviews[:2], []))
    ):
        result = asyncio.run(service.analyze(listing.url))
    assert result.status == "insufficient_evidence" and not requests
    assert result.usage.paid_attempts_today == 0


@pytest.mark.parametrize(
    "enabled,host,vercel,status",
    [
        (False, "127.0.0.1", False, 404),
        (True, "203.0.113.2", False, 403),
        (True, "127.0.0.1", True, 403),
    ],
)
def test_lab_access_is_independent_of_frontend_flags_and_spoofed_headers(
    wire, monkeypatch, enabled, host, vercel, status
):
    requests, _ = wire
    monkeypatch.setenv("NEXT_PUBLIC_DEV_MODE", "true")
    config = settings(langchain_lab_enabled=enabled, vercel=vercel)
    # No actual Atlas connection is involved in this access-control test.
    with patch("app.services.analysis.create_usage_store", return_value=UsageStore(settings())):
        app = create_app(config)

        async def run():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=(host, 1234)),
                    base_url="http://test",
                ) as client:
                    return await client.post(
                        "/api/v1/analysis/langchain",
                        json={"url": product().url},
                        headers={"X-Forwarded-For": "127.0.0.1", "Origin": "http://localhost:3000"},
                    )

        result = asyncio.run(run())
    assert result.status_code == status
    assert not requests
