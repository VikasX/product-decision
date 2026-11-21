import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from pymongo.errors import ConnectionFailure

from app.core.config import Settings
from app.db.session import get_database
from app.main import create_app


@pytest.mark.parametrize(
    ("path", "failed", "status", "body"),
    [
        ("/api/v1/health", False, 200, {"status": "ok"}),
        ("/api/v1/health/db", False, 200, {"status": "ok", "database": "mongodb"}),
        ("/api/v1/health/db", True, 503, {"detail": "Database unavailable"}),
    ],
)
def test_health(path: str, failed: bool, status: int, body: dict[str, str]) -> None:
    app = create_app(Settings(_env_file=None, mongodb_uri="mongodb://localhost:27017/"))
    database = AsyncMock()
    if failed:
        database.command.side_effect = ConnectionFailure("private connection details")
    app.dependency_overrides[get_database] = lambda: database

    async def request_health() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get(path)

    response = asyncio.run(request_health())
    assert response.status_code == status
    assert response.json() == body
    if path.endswith("/db"):
        database.command.assert_awaited_once_with("ping")
    else:
        database.command.assert_not_awaited()
