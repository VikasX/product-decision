import asyncio
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pymongo.errors import ConnectionFailure

from app.core.config import Settings
from app.main import create_app
from app.services.mongo_usage import MongoUsageStore
from app.services.usage import CostProtectionError, StorageUnavailableError, create_usage_store


def config(**kwargs):
    return Settings(_env_file=None, mongodb_uri="mongodb://localhost/", **kwargs)


def test_asgi_export_imports_without_config_clients_or_filesystem():
    import app.main as main

    with (
        patch.object(Settings, "__init__", side_effect=AssertionError("Settings at import")),
        patch(
            "app.services.analysis.AnalysisService.__init__",
            side_effect=AssertionError("Store at import"),
        ),
        patch("pathlib.Path.mkdir", side_effect=AssertionError("Filesystem at import")),
    ):
        assert isinstance(importlib.reload(main).app, FastAPI)


def test_vercel_forces_shared_storage_without_sqlite_files():
    with (
        patch("app.services.mongo_usage.MongoClient") as client,
        patch("pathlib.Path.mkdir", side_effect=AssertionError("SQLite write")),
    ):
        store = create_usage_store(config(vercel=True, ai_state_backend="sqlite"))
    assert isinstance(store, MongoUsageStore)
    assert client.call_args.kwargs["connect"] is False


def test_storage_failure_returns_503_before_openai():
    app = create_app(config(vercel=True))
    broken_store = MagicMock()
    broken_store.cached.side_effect = StorageUnavailableError("private storage details")

    with (
        patch("app.services.analysis.create_usage_store", return_value=broken_store),
        patch("app.integrations.llm.httpx.AsyncClient.post", AsyncMock()) as openai_post,
    ):
        # The patched method also handles our ASGI client, so use the original
        # transport-level request method to exercise the app route.
        openai_post.side_effect = AssertionError("Unexpected external POST")

        async def request():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    return await client.request(
                        "POST",
                        "/api/v1/analysis",
                        json={"url": "https://www.amazon.in/dp/B0D5Q7RWF6"},
                    )

        result = asyncio.run(request())
    assert result.status_code == 503
    assert "private storage details" not in result.text
    openai_post.assert_not_awaited()


def test_mongo_storage_errors_are_sanitized():
    with patch("app.services.mongo_usage.MongoClient"):
        store = MongoUsageStore(config())
    store.ledger.find_one.side_effect = ConnectionFailure("private URI")
    with pytest.raises(StorageUnavailableError, match="Shared AI budget storage"):
        store.usage()


def evaluate(expression, document):
    """Evaluate the query operators used by the single-document budget predicate."""
    if isinstance(expression, str) and expression.startswith("$"):
        return document[expression[1:]]
    if isinstance(expression, list):
        return [evaluate(value, document) for value in expression]
    if not isinstance(expression, dict):
        return expression
    operator, arguments = next(iter(expression.items()))
    values = evaluate(arguments, document)
    operations = {
        "$eq": lambda: values[0] == values[1],
        "$lte": lambda: values[0] <= values[1],
        "$lt": lambda: values[0] < values[1],
        "$add": lambda: sum(values),
        "$and": lambda: all(values),
        "$cond": lambda: values[1] if values[0] else values[2],
    }
    return operations[operator]()


class AtomicLedger:
    def __init__(self):
        self.document = None
        self.lock = threading.Lock()

    def update_one(self, query, update, **kwargs):
        with self.lock:
            if self.document is None:
                self.document = update["$setOnInsert"].copy()

    def find_one_and_update(self, query, update, **kwargs):
        with self.lock:
            if not evaluate(query["$expr"], self.document):
                return None
            changes = {
                key: evaluate(value, self.document) for key, value in update[0]["$set"].items()
            }
            self.document.update(changes)
            return self.document.copy()


def test_shared_mongo_predicate_prevents_concurrent_overspend_and_rolls_day():
    with patch("app.services.mongo_usage.MongoClient"):
        one = MongoUsageStore(config(ai_max_paid_attempts_per_day=1))
        two = MongoUsageStore(config(ai_max_paid_attempts_per_day=1))
    ledger = AtomicLedger()
    one.ledger = two.ledger = ledger

    def reserve(store):
        try:
            store.reserve(100)
            return True
        except CostProtectionError:
            return False

    with patch("app.services.mongo_usage.time.time", return_value=66599):
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sum(executor.map(reserve, [one, two])) == 1
    with patch("app.services.mongo_usage.time.time", return_value=66600):
        assert reserve(two)
    assert ledger.document["daily_reserved"] == 100
    assert ledger.document["lifetime_reserved"] == 200
    assert ledger.document["paid_attempts_today"] == 1


@pytest.mark.parametrize(
    "limits",
    [
        {"ai_daily_budget_usd": 0},
        {"ai_lifetime_budget_usd": 0},
    ],
)
def test_shared_mongo_predicate_checks_both_money_limits(limits):
    with patch("app.services.mongo_usage.MongoClient"):
        store = MongoUsageStore(config(**limits))
    store.ledger = AtomicLedger()
    with pytest.raises(CostProtectionError):
        store.reserve(100)
    assert store.ledger.document["lifetime_reserved"] == 0
