from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # Callers own transaction boundaries. Closing rolls back uncommitted work.
    async with request.app.state.session_factory() as session:
        yield session
