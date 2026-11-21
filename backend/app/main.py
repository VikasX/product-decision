import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import Settings
from app.db.session import create_client

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=config.log_level)
        client = create_client(config)
        app.state.database = client[config.mongodb_database]
        try:
            yield
        finally:
            await client.close()

    app = FastAPI(title="AI Product Review Intelligence Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin).rstrip("/") for origin in config.cors_origins],
        allow_methods=["GET"],
    )
    app.include_router(router)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error on %s", request.method, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app
