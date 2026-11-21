import hashlib
import sqlite3
import time
from contextlib import contextmanager

from app.core.config import Settings
from app.schemas.analysis import AnalysisResponse, Usage

PROMPT_VERSION = "review-v2"
NANODOLLARS = 1_000_000_000
# USD per token expressed as integer nanodollars. Reviewed 2026-11-22.
MODEL_PRICES = {
    "gpt-5-nano": (50, 400),
    "gpt-5-nano-2025-08-07": (50, 400),
}


class CostProtectionError(ValueError):
    pass


def credential_fingerprint(config: Settings) -> str:
    key = config.openai_api_key.get_secret_value() if config.openai_api_key else ""
    return hashlib.sha256(key.encode()).hexdigest()


class UsageStore:
    def __init__(self, config: Settings):
        self.config = config
        config.ai_state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
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

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.config.ai_state_path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def cached(self, key: str) -> AnalysisResponse | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT body FROM cache WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
        if row is None:
            return None
        return AnalysisResponse.model_validate_json(row[0])

    def finish(self, key: str, owner: str, cache_key: str, result: AnalysisResponse, ttl: int):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner FROM leases WHERE key=?", (key,)).fetchone()
            if row is None or row[0] != owner:
                return
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (cache_key, time.time() + ttl, result.model_dump_json()),
            )
            db.execute("DELETE FROM leases WHERE key=? AND owner=?", (key, owner))
            db.execute("DELETE FROM cache WHERE expires<?", (time.time(),))

    def acquire(self, key: str, owner: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expires<?", (time.time(),))
            cursor = db.execute(
                "INSERT OR IGNORE INTO leases VALUES (?, ?, ?)",
                (key, owner, time.time() + 240),
            )
            return cursor.rowcount == 1

    def release(self, key: str, owner: str):
        with self.connection() as db:
            db.execute("DELETE FROM leases WHERE key=? AND owner=?", (key, owner))

    def reject_bad_credential(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT 1 FROM auth_failures WHERE fingerprint=? AND expires>?",
                (credential_fingerprint(self.config), time.time()),
            ).fetchone()
        if row:
            raise CostProtectionError(
                "This API key was rejected recently. Update it or wait five minutes."
            )

    def record_auth_failure(self):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO auth_failures VALUES (?, ?)",
                (credential_fingerprint(self.config), time.time() + 300),
            )

    @staticmethod
    def totals(db, now: float) -> tuple[int, int, int]:
        # User-facing budget day resets at midnight Asia/Calcutta (UTC+05:30).
        offset = 19800
        day_start = int((now + offset) // 86400) * 86400 - offset
        daily, count = db.execute(
            "SELECT COALESCE(SUM(reserved), 0), COUNT(*) FROM attempts WHERE created>=?",
            (day_start,),
        ).fetchone()
        lifetime = db.execute("SELECT COALESCE(SUM(reserved), 0) FROM attempts").fetchone()[0]
        return daily, lifetime, count

    def reserve(self, amount: int):
        # Keep reservations even on timeout, invalid output, rejection or cancellation.
        # A failed attempt can still consume tokens. Never guess that a refund is safe.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            daily, lifetime, count = self.totals(db, time.time())
            if count >= self.config.ai_max_paid_attempts_per_day:
                raise CostProtectionError("Daily AI attempt limit reached. Try again tomorrow.")
            if daily + amount > int(self.config.ai_daily_budget_usd * NANODOLLARS):
                raise CostProtectionError(
                    "Daily AI budget reached. Cached results remain available."
                )
            if lifetime + amount > int(self.config.ai_lifetime_budget_usd * NANODOLLARS):
                raise CostProtectionError(
                    "Local AI spending cap reached. Review the budget settings."
                )
            db.execute(
                "INSERT INTO attempts (created, model, reserved) VALUES (?, ?, ?)",
                (time.time(), self.config.openai_model, amount),
            )

    def usage(self, reserved: int = 0, cached: bool = False) -> Usage:
        with self.connection() as db:
            daily, lifetime, count = self.totals(db, time.time())
        return Usage(
            model=self.config.openai_model,
            reserved_cost_usd=reserved / NANODOLLARS,
            daily_reserved_usd=daily / NANODOLLARS,
            lifetime_reserved_usd=lifetime / NANODOLLARS,
            paid_attempts_today=count,
            cached=cached,
        )
