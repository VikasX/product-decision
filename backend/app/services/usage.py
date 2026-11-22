import hashlib
import sqlite3
import time
from contextlib import contextmanager

from app.core.config import Settings
from app.schemas.analysis import AnalysisResponse, Usage

# Included in cache keys so changed evidence/prompt rules cannot reuse older judgments.
PROMPT_VERSION = "review-v6-product-widget"
# Store money as integer billionths of a dollar to avoid floating-point rounding during cap checks.
NANODOLLARS = 1_000_000_000
# USD per token expressed as integer nanodollars. Reviewed 2026-11-22.
MODEL_PRICES = {
    "gpt-5-nano": (50, 400),
    "gpt-5-nano-2025-08-07": (50, 400),
}


# Expected pre-request rejection for configuration, size limits or app-side spending caps.
class CostProtectionError(ValueError):
    pass


# Missing durable budget state blocks new spending and becomes HTTP 503.
class StorageUnavailableError(RuntimeError):
    pass


# Vercel uses shared MongoDB; default local auto mode uses persistent SQLite.
def create_usage_store(config: Settings):
    # Choose shared storage on Vercel; a local file cannot coordinate spending across server instances.
    if config.use_mongo_state:
        from app.services.mongo_usage import MongoUsageStore

        return MongoUsageStore(config)
    return UsageStore(config)


# Hash the key for brief authentication lockouts without persisting the raw secret.
def credential_fingerprint(config: Settings) -> str:
    # Read the secret only to hash it; the raw key is not the database identifier.
    key = config.openai_api_key.get_secret_value() if config.openai_api_key else ""
    return hashlib.sha256(key.encode()).hexdigest()


# Local SQLite implementation of the shared cache/lease/reservation interface.
class UsageStore:
    def __init__(self, config: Settings):
        # Keep settings such as caps, model and timeouts available to every store operation.
        self.config = config
        # Create the local state folder if needed without deleting existing usage history.
        config.ai_state_path.parent.mkdir(parents=True, exist_ok=True)
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # WAL allows readers and short writes to coexist more easily in the local database.
            db.execute("PRAGMA journal_mode=WAL")
            # Create cache, lease, attempt and rejected-key tables only if they do not already exist.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, expires REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL,
                    model TEXT NOT NULL, reserved INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_failures (
                    fingerprint TEXT PRIMARY KEY, expires REAL NOT NULL
                );
            """)

    # Turn this generator into a with-block helper; cleanup/error handling runs around the caller work.
    @contextmanager
    # Short transactions commit or roll back, then close the connection.
    def connection(self):
        # Connect to the configured file and wait at most five seconds for a local database lock.
        db = sqlite3.connect(self.config.ai_state_path, timeout=5)
        try:
            # SQLite commits the block on success or rolls it back if an exception escapes.
            with db:
                # Temporarily give the open database connection to the surrounding with block.
                yield db
        finally:
            db.close()

    # Release resources owned by this store when application shutdown calls close.
    def close(self):
        pass

    # Return only unexpired reports, validating stored JSON against the API schema.
    def cached(self, key: str) -> AnalysisResponse | None:
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # Read the one matching row; fetchone returns None when no row matches.
            row = db.execute(
                "SELECT body FROM cache WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
        # A missing/expired cache row means the caller must continue rather than reuse a report.
        if row is None:
            return None
        # Validate the saved report JSON before returning it to the service.
        return AnalysisResponse.model_validate_json(row[0])

    # Only the lease owner may publish a result, preventing stale-worker overwrites.
    def finish(self, key: str, owner: str, cache_key: str, result: AnalysisResponse, ttl: int):
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # Acquire the write transaction before checking shared state so another writer cannot race this operation.
            db.execute("BEGIN IMMEDIATE")
            # Read the one matching row; fetchone returns None when no row matches.
            row = db.execute("SELECT owner FROM leases WHERE key=?", (key,)).fetchone()
            # Only the current lease owner may save a finished report.
            if row is None or row[0] != owner:
                return
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (cache_key, time.time() + ttl, result.model_dump_json()),
            )
            db.execute("DELETE FROM leases WHERE key=? AND owner=?", (key, owner))
            db.execute("DELETE FROM cache WHERE expires<?", (time.time(),))

    # Atomic insert grants one expiring lease per product/workflow to suppress duplicate calls.
    def acquire(self, key: str, owner: str) -> bool:
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # Acquire the write transaction before checking shared state so another writer cannot race this operation.
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expires<?", (time.time(),))
            # Attempt a unique lease insert; INSERT OR IGNORE leaves another live lease untouched.
            cursor = db.execute(
                "INSERT OR IGNORE INTO leases VALUES (?, ?, ?)",
                (key, owner, time.time() + 240),
            )
            # A newly inserted row means this worker obtained the lease.
            return cursor.rowcount == 1

    # Remove a lease only when its owner matches this worker; do not unlock another worker task.
    def release(self, key: str, owner: str):
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            db.execute("DELETE FROM leases WHERE key=? AND owner=?", (key, owner))

    # Avoid repeating requests for a key that recently received HTTP 401.
    # Read the short rejection record before reserving another paid attempt.
    def reject_bad_credential(self):
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # Read the one matching row; fetchone returns None when no row matches.
            row = db.execute(
                "SELECT 1 FROM auth_failures WHERE fingerprint=? AND expires>?",
                (credential_fingerprint(self.config), time.time()),
            ).fetchone()
        # A recent authentication rejection blocks another attempt until its expiry or key replacement.
        if row:
            raise CostProtectionError(
                "This API key was rejected recently. Update it or wait five minutes."
            )

    # Remember this key fingerprint for five minutes after OpenAI rejects its authentication.
    def record_auth_failure(self):
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO auth_failures VALUES (?, ?)",
                (credential_fingerprint(self.config), time.time() + 300),
            )

    @staticmethod
    def totals(db, now: float) -> tuple[int, int, int]:
        # User-facing budget day resets at midnight Asia/Calcutta (UTC+05:30).
        # Asia/Calcutta is 19,800 seconds ahead of UTC; use it to calculate the user-facing day boundary.
        offset = 19800
        day_start = int((now + offset) // 86400) * 86400 - offset
        # Sum reservations and count paid attempts since the current budget day began.
        daily, count = db.execute(
            "SELECT COALESCE(SUM(reserved), 0), COUNT(*) FROM attempts WHERE created>=?",
            (day_start,),
        ).fetchone()
        # Sum the entire attempt history; daily rollover does not clear this number.
        lifetime = db.execute("SELECT COALESCE(SUM(reserved), 0) FROM attempts").fetchone()[0]
        return daily, lifetime, count

    # Check all caps and persist a maximum-cost reservation atomically before the model call.
    def reserve(self, amount: int):
        # Keep reservations even on timeout, invalid output, rejection or cancellation.
        # A failed attempt can still consume tokens. Never guess that a refund is safe.
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # Acquire the write transaction before checking shared state so another writer cannot race this operation.
            db.execute("BEGIN IMMEDIATE")
            # Read all three budget counters before deciding whether a new reservation is allowed.
            daily, lifetime, count = self.totals(db, time.time())
            # Stop when the daily attempt count is exhausted, even if the dollar limits have room.
            if count >= self.config.ai_max_paid_attempts_per_day:
                raise CostProtectionError("Daily AI attempt limit reached. Try again tomorrow.")
            # Include the next conservative reservation when checking the daily dollar cap.
            if daily + amount > int(self.config.ai_daily_budget_usd * NANODOLLARS):
                raise CostProtectionError(
                    "Daily AI budget reached. Cached results remain available."
                )
            # Include the same reservation when checking the store lifetime cap.
            if lifetime + amount > int(self.config.ai_lifetime_budget_usd * NANODOLLARS):
                raise CostProtectionError(
                    "Local AI spending cap reached. Review the budget settings."
                )
            db.execute(
                # Persist the attempt before the model request; failures do not automatically remove this row.
                "INSERT INTO attempts (created, model, reserved) VALUES (?, ?, ?)",
                (time.time(), self.config.openai_model, amount),
            )

    # These conservative reservations are not reconciled OpenAI billing amounts.
    def usage(self, reserved: int = 0, cached: bool = False) -> Usage:
        # Open a short SQLite connection/transaction; the helper closes it after this operation.
        with self.connection() as db:
            # Read all three budget counters before deciding whether a new reservation is allowed.
            daily, lifetime, count = self.totals(db, time.time())
        return Usage(
            model=self.config.openai_model,
            # Convert this attempt integer nanodollar reservation into dollars for the API report.
            reserved_cost_usd=reserved / NANODOLLARS,
            daily_reserved_usd=daily / NANODOLLARS,
            # Lifetime means all recorded reservations in this store, including failed attempts.
            lifetime_reserved_usd=lifetime / NANODOLLARS,
            paid_attempts_today=count,
            cached=cached,
        )
