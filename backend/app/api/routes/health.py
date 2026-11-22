import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.db.diagnostics import log_mongo_failure
from app.db.session import get_database

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db", tags=["health"], responses={503: {"description": "Database unavailable"}})
async def database_health(
    database: Annotated[AsyncDatabase[dict[str, Any]], Depends(get_database)],
) -> dict[str, str]:
    try:
        await database.command("ping")
    except PyMongoError as exc:
        log_mongo_failure(logger, "health_ping", exc)
        # Keep connection details and credentials out of API responses.
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok", "database": "mongodb"}
