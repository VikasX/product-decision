import hashlib
import logging
import time
from contextlib import contextmanager

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

from app.core.config import Settings
from app.db.diagnostics import log_mongo_failure
from app.schemas.analysis import AnalysisResponse, Usage
from app.services.usage import (
    NANODOLLARS,
    CostProtectionError,
    StorageUnavailableError,
    credential_fingerprint,
)

logger = logging.getLogger(__name__)


class MongoUsageStore:
    """Shared budgets and results for stateless hosts, using single-document atomic writes."""

    def __init__(self, config: Settings):
        self.config = config
        with self.operation():
            self.client = MongoClient(
                config.mongodb_uri.get_secret_value(),
                connect=False,
                timeoutMS=config.mongodb_timeout_ms,
                serverSelectionTimeoutMS=config.mongodb_timeout_ms,
            )
            database = self.client[config.mongodb_database]
            durable = WriteConcern(w="majority")
            self.ledger = database.get_collection("ai_usage_ledger", write_concern=durable)
            self.state = database.get_collection("ai_analysis_state", write_concern=durable)
            self.auth = database.get_collection("ai_auth_failures", write_concern=durable)

    @contextmanager
    def operation(self):
        try:
            yield
        except PyMongoError as exc:
            log_mongo_failure(logger, "usage_store", exc)
            raise StorageUnavailableError(
                "Shared AI budget storage is unavailable. No new AI request can be made."
            ) from None

    @staticmethod
    def state_id(key: str) -> str:
        product_key = key.split(":failure:", 1)[0]
        return hashlib.sha256(product_key.encode()).hexdigest()

    @staticmethod
    def day_start(now: float) -> int:
        return int((now + 19800) // 86400) * 86400 - 19800

    def cached(self, key: str) -> AnalysisResponse | None:
        with self.operation():
            row = self.state.find_one(
                {"_id": self.state_id(key), "cache.key": key, "cache.expires": {"$gt": time.time()}}
            )
        if row is None:
            return None
        return AnalysisResponse.model_validate_json(row["cache"]["body"])

    def acquire(self, key: str, owner: str) -> bool:
        with self.operation():
            try:
                row = self.state.find_one_and_update(
                    {
                        "_id": self.state_id(key),
                        "$or": [
                            {"lease_until": {"$lte": time.time()}},
                            {"lease_until": {"$exists": False}},
                        ],
                    },
                    {"$set": {"owner": owner, "lease_until": time.time() + 240}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                # The unique _id already belongs to another unexpired lease.
                return False
        return row is not None

    def finish(self, key: str, owner: str, cache_key: str, result: AnalysisResponse, ttl: int):
        with self.operation():
            self.state.update_one(
                {"_id": self.state_id(key), "owner": owner, "lease_until": {"$gt": time.time()}},
                {
                    "$set": {
                        "cache": {
                            "key": cache_key,
                            "expires": time.time() + ttl,
                            "body": result.model_dump_json(),
                        }
                    },
                    "$unset": {"owner": "", "lease_until": ""},
                },
            )

    def release(self, key: str, owner: str):
        with self.operation():
            self.state.update_one(
                {"_id": self.state_id(key), "owner": owner},
                {"$unset": {"owner": "", "lease_until": ""}},
            )

    def reject_bad_credential(self):
        with self.operation():
            row = self.auth.find_one(
                {"_id": credential_fingerprint(self.config), "expires": {"$gt": time.time()}}
            )
        if row is not None:
            raise CostProtectionError(
                "This API key was rejected recently. Update it or wait five minutes."
            )

    def record_auth_failure(self):
        with self.operation():
            self.auth.update_one(
                {"_id": credential_fingerprint(self.config)},
                {"$set": {"expires": time.time() + 300}},
                upsert=True,
            )

    def reserve(self, amount: int):
        today = self.day_start(time.time())
        same_day = {"$eq": ["$day", today]}
        daily = {"$cond": [same_day, "$daily_reserved", 0]}
        attempts = {"$cond": [same_day, "$paid_attempts_today", 0]}
        with self.operation():
            # Initialise once. Competing instances can race to create this unique _id.
            try:
                self.ledger.update_one(
                    {"_id": "budget"},
                    {
                        "$setOnInsert": {
                            "day": today,
                            "daily_reserved": 0,
                            "lifetime_reserved": 0,
                            "paid_attempts_today": 0,
                        }
                    },
                    upsert=True,
                )
            except DuplicateKeyError:
                pass
            row = self.ledger.find_one_and_update(
                {
                    "_id": "budget",
                    "$expr": {
                        "$and": [
                            {
                                "$lte": [
                                    {"$add": ["$lifetime_reserved", amount]},
                                    int(self.config.ai_lifetime_budget_usd * NANODOLLARS),
                                ]
                            },
                            {
                                "$lte": [
                                    {"$add": [daily, amount]},
                                    int(self.config.ai_daily_budget_usd * NANODOLLARS),
                                ]
                            },
                            {"$lt": [attempts, self.config.ai_max_paid_attempts_per_day]},
                        ]
                    },
                },
                [
                    {
                        "$set": {
                            "day": today,
                            "daily_reserved": {"$add": [daily, amount]},
                            "lifetime_reserved": {"$add": ["$lifetime_reserved", amount]},
                            "paid_attempts_today": {"$add": [attempts, 1]},
                        }
                    }
                ],
                return_document=ReturnDocument.AFTER,
            )
        if row is None:
            raise CostProtectionError(
                "AI budget or daily attempt limit reached. Cached results remain available."
            )

    def usage(self, reserved: int = 0, cached: bool = False) -> Usage:
        with self.operation():
            row = self.ledger.find_one({"_id": "budget"}) or {}
        same_day = row.get("day") == self.day_start(time.time())
        return Usage(
            model=self.config.openai_model,
            reserved_cost_usd=reserved / NANODOLLARS,
            daily_reserved_usd=row.get("daily_reserved", 0) / NANODOLLARS if same_day else 0,
            lifetime_reserved_usd=row.get("lifetime_reserved", 0) / NANODOLLARS,
            paid_attempts_today=row.get("paid_attempts_today", 0) if same_day else 0,
            cached=cached,
        )

    def close(self):
        self.client.close()
