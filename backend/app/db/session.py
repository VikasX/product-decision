from typing import Any

from fastapi import Request
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings


# App-owned async pool for DB dependencies/health; usage stores own their ledger client.
def create_client(settings: Settings) -> AsyncMongoClient[dict[str, Any]]:
    # Return a pool-backed client, with each connection/operation phase bounded by the configured timeout.
    return AsyncMongoClient(
        settings.mongodb_uri.get_secret_value(),
        serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
        connectTimeoutMS=settings.mongodb_timeout_ms,
        socketTimeoutMS=settings.mongodb_timeout_ms,
        timeoutMS=settings.mongodb_timeout_ms,
    )


async def get_database(request: Request) -> AsyncDatabase[dict[str, Any]]:
    """Reuse the application client and its connection pool across requests."""
    # Retrieve the handle created during FastAPI startup; no new pool is created per request.
    return request.app.state.database
