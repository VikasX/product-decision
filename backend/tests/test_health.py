import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.main import create_app


def test_health(monkeypatch: pytest.MonkeyPatch) -> None:
    # Liveness must succeed even when there is no running database.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    app = create_app(Settings(_env_file=None))

    async def request_health() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get("/api/v1/health")

    response = asyncio.run(request_health())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
