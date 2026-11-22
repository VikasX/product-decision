"""Run with python -m app.db.check from the backend directory."""

import asyncio

from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app.core.config import Settings
from app.db.session import create_client


# Manual CLI diagnostic returns an exit code without exposing database credentials.
async def check_connection() -> int:
    try:
        # Load the real environment configuration for this explicitly run manual check.
        settings = Settings()
        # Create and automatically close a temporary async MongoDB client for the ping.
        async with create_client(settings) as client:
            await client[settings.mongodb_database].command("ping")
        print("MongoDB connection successful.")
        # Exit code zero tells a terminal or script that the connection check succeeded.
        return 0
    # Handle database and configuration errors without printing their sensitive original details.
    except (PyMongoError, ValidationError, ValueError):
        print(
            "MongoDB connection failed. Check MONGODB_URI, database user credentials, "
            "Atlas IP access, DNS, and cluster availability."
        )
        # Return a nonzero exit code so callers can detect failure.
        return 1


# Run this diagnostic only when invoked as a script/module, not when another module imports it.
if __name__ == "__main__":
    raise SystemExit(asyncio.run(check_connection()))
