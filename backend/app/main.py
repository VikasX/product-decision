import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import CorsSettings, Settings
from app.db.session import create_client
from app.services.analysis import AnalysisService
from app.services.usage import StorageUnavailableError

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    cors_origins = settings.cors_origins if settings else CorsSettings().cors_origins

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings or Settings()
        logging.basicConfig(level=config.log_level)
        client = create_client(config)
        app.state.database = client[config.mongodb_database]
        try:
            app.state.analysis_service = AnalysisService(config)
            if config.langchain_lab_enabled and not config.vercel:
                from app.services.langchain_analysis import LangChainAnalysisService

                app.state.langchain_analysis_service = LangChainAnalysisService(
                    config, store=app.state.analysis_service.store
                )
            yield
        finally:
            service = getattr(app.state, "analysis_service", None)
            if service is not None:
                service.store.close()
            await client.close()

    app = FastAPI(title="AI Product Review Intelligence Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(
            {
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "https://product-decision-ui.vercel.app",
                *(str(origin).rstrip("/") for origin in cors_origins),
            }
        ),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.include_router(router)

    @app.exception_handler(StorageUnavailableError)
    async def storage_unavailable(request: Request, exc: StorageUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Shared AI budget storage is unavailable. Please try again later."},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error on %s", request.method, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


# Vercel discovers this ASGI object. Configuration and clients initialise at startup.
app = create_app()
