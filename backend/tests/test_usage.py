import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.llm import analyze_reviews, prepare_request
from app.schemas.analysis import Product, Review
from app.services.analysis import AnalysisService
from app.services.usage import CostProtectionError, UsageStore


def config(**kwargs):
    return Settings(
        _env_file=None, mongodb_uri="mongodb://localhost/", openai_api_key="test", **kwargs
    )


def evidence():
    product = Product(asin="B0D5Q7RWF6", title="LED", url="https://www.amazon.in/dp/B0D5Q7RWF6")
    reviews = [
        Review(id=f"R{index}", text=f"Glue fails on unit {index}", stars=1, source_url=product.url)
        for index in range(3)
    ]
    return product, reviews


def response(content=None, status=200):
    content = content or json.dumps(
        {
            "verdict": "proceed_with_caution",
            "summary": "Repeated adhesive complaints in this limited sample.",
            "positive_findings": [],
            "amazon_findings": [],
            "negative_findings": [
                {
                    "text": "Weak adhesive",
                    "review_ids": ["R0", "R1"],
                    "evidence": [
                        {"review_id": item, "quote": "Glue fails"} for item in ["R0", "R1"]
                    ],
                }
            ],
            "recurring_issues": [],
            "reliability_findings": [],
            "key_takeaway": "Check the cited evidence before buying.",
        }
    )
    return httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.openai.com"),
        json={"choices": [{"message": {"content": content}}]},
    )


@pytest.mark.parametrize(
    "setting",
    [
        {"ai_daily_budget_usd": 0},
        {"ai_lifetime_budget_usd": 0},
        {"ai_max_paid_attempts_per_day": 0},
        {"openai_max_input_tokens": 1000},
    ],
)
def test_limits_block_before_network(setting):
    product, reviews = evidence()
    with patch("httpx.AsyncClient.post", AsyncMock()) as post:
        with pytest.raises(CostProtectionError):
            asyncio.run(analyze_reviews(config(**setting), product, reviews))
    post.assert_not_awaited()
    assert UsageStore(config()).usage().paid_attempts_today == 0


def test_unknown_model_rejected():
    with pytest.raises(ValidationError):
        config(openai_model="unpriced-model")


def test_request_uses_minimal_reasoning_and_total_output_cap():
    product, reviews = evidence()
    payload, cost = prepare_request(config(), product, reviews)
    assert payload["model"] == "gpt-5-nano"
    assert payload["reasoning_effort"] == "minimal"
    assert payload["max_completion_tokens"] == 2000
    assert cost > 2000 * 400


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        ('{"verdict":"skip","summary":"Bad","reasons":[{"text":"Bad","review_ids":["invented"]}]}'),
    ],
)
def test_invalid_output_still_counts_and_never_retries(content):
    product, reviews = evidence()
    settings = config()
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=response(content))) as post:
        with pytest.raises(ValueError):
            asyncio.run(analyze_reviews(settings, product, reviews))
    assert post.await_count == 1
    usage = UsageStore(settings).usage()
    assert usage.paid_attempts_today == 1
    assert usage.lifetime_reserved_usd > 0


def test_auth_failure_blocks_other_products_until_key_changes():
    product, reviews = evidence()
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=response(status=401))) as post:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(analyze_reviews(config(), product, reviews))
        product.asin = "B012345678"
        with pytest.raises(CostProtectionError, match="rejected recently"):
            asyncio.run(analyze_reviews(config(), product, reviews))
    assert post.await_count == 1
    new_config = config()
    new_config.openai_api_key = type(new_config.openai_api_key)("new-test-key")
    UsageStore(new_config).reject_bad_credential()


def test_verified_uncertain_verdict_cached_across_service_restart_even_when_budget_zero():
    product, reviews = evidence()
    uncertain = json.dumps(
        {
            "verdict": "insufficient_evidence",
            "summary": "Need more reviews.",
            "positive_findings": [],
            "amazon_findings": [],
            "negative_findings": [],
            "recurring_issues": [],
            "reliability_findings": [],
            "key_takeaway": "Check the cited evidence before buying.",
        }
    )
    with (
        patch(
            "app.services.analysis.collect_reviews", AsyncMock(return_value=(product, reviews, []))
        ) as collect,
        patch("httpx.AsyncClient.post", AsyncMock(return_value=response(uncertain))) as post,
    ):
        first = asyncio.run(AnalysisService(config()).analyze(product.url))
        second = asyncio.run(AnalysisService(config(ai_daily_budget_usd=0)).analyze(product.url))
    assert first.verdict == "insufficient_evidence"
    assert first.usage.reserved_cost_usd > 0
    assert second.cached
    assert second.usage.reserved_cost_usd == 0
    assert post.await_count == 1
    assert collect.await_count == 1


def test_simultaneous_services_deduplicate_same_product():
    product, reviews = evidence()

    async def delayed_response(*args, **kwargs):
        await asyncio.sleep(0.05)
        return response()

    async def run():
        one = AnalysisService(config())
        two = AnalysisService(config())
        return await asyncio.gather(one.analyze(product.url), two.analyze(product.url))

    with (
        patch(
            "app.services.analysis.collect_reviews", AsyncMock(return_value=(product, reviews, []))
        ),
        patch("httpx.AsyncClient.post", AsyncMock(side_effect=delayed_response)) as post,
    ):
        results = asyncio.run(run())
    assert sum(result.cached for result in results) == 1
    assert post.await_count == 1
    assert UsageStore(config()).usage().paid_attempts_today == 1


def test_budget_reservations_are_atomic_across_connections():
    one = UsageStore(config(ai_max_paid_attempts_per_day=1))
    two = UsageStore(config(ai_max_paid_attempts_per_day=1))

    def reserve(store):
        try:
            store.reserve(100)
            return True
        except CostProtectionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, [one, two]))
    assert sum(outcomes) == 1
    assert one.usage().paid_attempts_today == 1


def test_budget_day_resets_at_midnight_india():
    store = UsageStore(config())
    # Epoch 66600 is 00:00 on January 2 in India, still January 1 in UTC.
    with patch("app.services.usage.time.time", return_value=66599):
        store.reserve(100)
        assert store.usage().paid_attempts_today == 1
    with patch("app.services.usage.time.time", return_value=66600):
        assert store.usage().paid_attempts_today == 0
        assert store.usage().lifetime_reserved_usd > 0
