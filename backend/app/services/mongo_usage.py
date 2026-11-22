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
        # Keep settings such as caps, model and timeouts available to every store operation.
        self.config = config
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            # This store owns a synchronous MongoDB client for atomic budget/cache operations.
            self.client = MongoClient(
                config.mongodb_uri.get_secret_value(),
                # Delay connecting until an operation needs MongoDB; app creation alone does not prove connectivity.
                connect=False,
                timeoutMS=config.mongodb_timeout_ms,
                serverSelectionTimeoutMS=config.mongodb_timeout_ms,
            )
            database = self.client[config.mongodb_database]
            # Require majority acknowledgement for state writes so a successful reservation is durably recorded.
            durable = WriteConcern(w="majority")
            # Use one shared budget collection for both HTTP and LangChain attempts.
            self.ledger = database.get_collection("ai_usage_ledger", write_concern=durable)
            # Keep versioned product reports and their processing leases in the shared state collection.
            self.state = database.get_collection("ai_analysis_state", write_concern=durable)
            # Keep temporary authentication rejection records separate from product reports.
            self.auth = database.get_collection("ai_auth_failures", write_concern=durable)

    # Turn this generator into a with-block helper; cleanup/error handling runs around the caller work.
    @contextmanager
    # Convert storage failures to a safe fail-closed signal without exposing connection details.
    def operation(self):
        try:
            yield
        # Never silently fall back to a fresh local budget after a shared storage failure.
        except PyMongoError as exc:
            log_mongo_failure(logger, "usage_store", exc)
            raise StorageUnavailableError(
                "Shared AI budget storage is unavailable. No new AI request can be made."
            ) from None

    @staticmethod
    # Success/failure keys share a document; workflow/version namespaces still remain distinct.
    def state_id(key: str) -> str:
        # Remove the failure/credential suffix so each product workflow uses one lease/cache document.
        product_key = key.split(":failure:", 1)[0]
        return hashlib.sha256(product_key.encode()).hexdigest()

    @staticmethod
    # Budget days follow Asia/Calcutta midnight, matching local SQLite accounting.
    def day_start(now: float) -> int:
        return int((now + 19800) // 86400) * 86400 - 19800

    # Enforce expiry on reads even without background TTL cleanup.
    def cached(self, key: str) -> AnalysisResponse | None:
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            # Look for the exact cache key with an expiry still in the future.
            row = self.state.find_one(
                {"_id": self.state_id(key), "cache.key": key, "cache.expires": {"$gt": time.time()}}
            )
        # No matching ledger update means at least one configured spending/attempt cap prevented this call.
        if row is None:
            return None
        return AnalysisResponse.model_validate_json(row["cache"]["body"])

    # Conditional writes lease one product across separate Vercel instances.
    def acquire(self, key: str, owner: str) -> bool:
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            try:
                # Ask MongoDB to claim an expired/missing lease and return the updated document atomically.
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
            # A competing request may have created the unique document first; handle that expected race.
            except DuplicateKeyError:
                # The unique _id already belongs to another unexpired lease.
                return False
        return row is not None

    # Publish only while the worker owns a live lease, using the service-supplied cache TTL.
    def finish(self, key: str, owner: str, cache_key: str, result: AnalysisResponse, ttl: int):
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            # Update only the document/owner matched by the filter below.
            self.state.update_one(
                {"_id": self.state_id(key), "owner": owner, "lease_until": {"$gt": time.time()}},
                {
                    # Write updated fields only after the matching conditions have succeeded.
                    "$set": {
                        "cache": {
                            "key": cache_key,
                            "expires": time.time() + ttl,
                            "body": result.model_dump_json(),
                        }
                    },
                    # Remove lease ownership fields so a future request can acquire this product again.
                    "$unset": {"owner": "", "lease_until": ""},
                },
            )

    # Remove a lease only when its owner matches this worker; do not unlock another worker task.
    def release(self, key: str, owner: str):
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            # Update only the document/owner matched by the filter below.
            self.state.update_one(
                {"_id": self.state_id(key), "owner": owner},
                {"$unset": {"owner": "", "lease_until": ""}},
            )

    # Read the short rejection record before reserving another paid attempt.
    def reject_bad_credential(self):
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            row = self.auth.find_one(
                {"_id": credential_fingerprint(self.config), "expires": {"$gt": time.time()}}
            )
        if row is not None:
            raise CostProtectionError(
                "This API key was rejected recently. Update it or wait five minutes."
            )

    # Remember this key fingerprint for five minutes after OpenAI rejects its authentication.
    def record_auth_failure(self):
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            self.auth.update_one(
                {"_id": credential_fingerprint(self.config)},
                {"$set": {"expires": time.time() + 300}},
                upsert=True,
            )

    # One atomic ledger update checks caps and increments reservations for both model workflows.
    def reserve(self, amount: int):
        # Calculate this budget day start once for the reservation operation.
        today = self.day_start(time.time())
        # Build a MongoDB expression that checks whether stored daily counters belong to today.
        same_day = {"$eq": ["$day", today]}
        # Use the stored daily amount for today, or zero after the day changes.
        daily = {"$cond": [same_day, "$daily_reserved", 0]}
        # Use today stored attempt count, or zero after daily rollover.
        attempts = {"$cond": [same_day, "$paid_attempts_today", 0]}
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            # Initialise once. Competing instances can race to create this unique _id.
            try:
                self.ledger.update_one(
                    {"_id": "budget"},
                    {
                        # Initialize a new budget document without overwriting an existing ledger.
                        "$setOnInsert": {
                            "day": today,
                            "daily_reserved": 0,
                            "lifetime_reserved": 0,
                            "paid_attempts_today": 0,
                        }
                    },
                    upsert=True,
                )
            # A competing request may have created the unique document first; handle that expected race.
            except DuplicateKeyError:
                pass
            # Combine cap checks and increments in one atomic write so concurrent requests cannot overspend.
            # Read current shared totals; a missing ledger means nothing has been reserved yet.
            row = self.ledger.find_one_and_update(
                {
                    "_id": "budget",
                    # Evaluate arithmetic conditions on the current stored counters inside MongoDB.
                    "$expr": {
                        # Every lifetime, daily and attempt-count condition must pass before a reservation is recorded.
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
                        # Write updated fields only after the matching conditions have succeeded.
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
        # No matching ledger update means at least one configured spending/attempt cap prevented this call.
        if row is None:
            raise CostProtectionError(
                "AI budget or daily attempt limit reached. Cached results remain available."
            )

    # Failures retain reservations since a failed request may still consume paid tokens.
    def usage(self, reserved: int = 0, cached: bool = False) -> Usage:
        # Wrap MongoDB work so database failures become safe storage-unavailable errors.
        with self.operation():
            # Read current shared totals; a missing ledger means nothing has been reserved yet.
            row = self.ledger.find_one({"_id": "budget"}) or {}
        # Build a MongoDB expression that checks whether stored daily counters belong to today.
        # Old daily totals are displayed as zero after midnight, without erasing lifetime history.
        same_day = row.get("day") == self.day_start(time.time())
        return Usage(
            model=self.config.openai_model,
            # Convert this attempt integer nanodollar reservation into dollars for the API report.
            reserved_cost_usd=reserved / NANODOLLARS,
            daily_reserved_usd=row.get("daily_reserved", 0) / NANODOLLARS if same_day else 0,
            # Lifetime means all recorded reservations in this store, including failed attempts.
            lifetime_reserved_usd=row.get("lifetime_reserved", 0) / NANODOLLARS,
            paid_attempts_today=row.get("paid_attempts_today", 0) if same_day else 0,
            cached=cached,
        )

    # Release resources owned by this store when application shutdown calls close.
    def close(self):
        self.client.close()
