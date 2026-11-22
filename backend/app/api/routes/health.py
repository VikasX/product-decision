import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.db.diagnostics import log_mongo_failure
from app.db.session import get_database

router = APIRouter()
logger = logging.getLogger(__name__)


# Liveness only; this does not prove Amazon, OpenAI or MongoDB is available.
@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    # Return a small success response; this route does not collect reviews or call OpenAI.
    return {"status": "ok"}


# Database readiness check only: no product analysis or paid AI request.
@router.get("/health/db", tags=["health"], responses={503: {"description": "Database unavailable"}})
async def database_health(
    # Depends asks FastAPI to obtain the shared database handle from get_database for this request.
    database: Annotated[AsyncDatabase[dict[str, Any]], Depends(get_database)],
) -> dict[str, str]:
    try:
        # Wait for a real MongoDB ping without running a product analysis.
        await database.command("ping")
    # Catch database-specific failures and return a safe error rather than connection details.
    except PyMongoError as exc:
        log_mongo_failure(logger, "health_ping", exc)
        # Keep connection details and credentials out of API responses.
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    # Return a small success response; this route does not collect reviews or call OpenAI.
    return {"status": "ok", "database": "mongodb"}
