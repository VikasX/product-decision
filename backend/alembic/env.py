import asyncio

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import Settings
from app.db.base import Base

url = Settings().database_url.get_secret_value()


def migrate(connection: Connection | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=Base.metadata,
        literal_binds=connection is None,
    )
    with context.begin_transaction():
        context.run_migrations()


async def migrate_online() -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    migrate()
else:
    asyncio.run(migrate_online())
