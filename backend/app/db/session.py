from typing import Any

from fastapi import Request
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings


def create_client(settings: Settings) -> AsyncMongoClient[dict[str, Any]]:
    return AsyncMongoClient(
        settings.mongodb_uri.get_secret_value(),
        serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
        connectTimeoutMS=settings.mongodb_timeout_ms,
        socketTimeoutMS=settings.mongodb_timeout_ms,
        timeoutMS=settings.mongodb_timeout_ms,
    )


async def get_database(request: Request) -> AsyncDatabase[dict[str, Any]]:
    """Reuse the application client and its connection pool across requests."""
    return request.app.state.database
