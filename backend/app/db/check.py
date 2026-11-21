"""Run with python -m app.db.check from the backend directory."""

import asyncio

from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app.core.config import Settings
from app.db.session import create_client


async def check_connection() -> int:
    try:
        settings = Settings()
        async with create_client(settings) as client:
            await client[settings.mongodb_database].command("ping")
        print("MongoDB connection successful.")
        return 0
    except (PyMongoError, ValidationError, ValueError):
        print(
            "MongoDB connection failed. Check MONGODB_URI, database user credentials, "
            "Atlas IP access, DNS, and cluster availability."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check_connection()))
