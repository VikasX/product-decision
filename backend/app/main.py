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

# Use this module name to identify where an unexpected server error came from.
logger = logging.getLogger(__name__)


# Build the ASGI app; injected settings let tests avoid real service configuration.
def create_app(settings: Settings | None = None) -> FastAPI:
    # Tests may pass settings directly. Otherwise read the allowed browser origins from the environment.
    cors_origins = settings.cors_origins if settings else CorsSettings().cors_origins

    # Startup owns shared clients/services; shutdown releases their connections.
    # This decorator makes the function a startup/shutdown context manager.
    # Code before yield runs at startup; the finally block runs when the app stops.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Load typed environment settings once during startup, rather than once per request.
        config = settings or Settings()
        # Apply the configured log level before creating the application services.
        logging.basicConfig(level=config.log_level)
        # Create one async MongoDB client whose connection pool is reused by database routes.
        client = create_client(config)
        # Store the database handle on the app so request dependencies can retrieve it later.
        app.state.database = client[config.mongodb_database]
        try:
            # The normal endpoint uses this HTTP-based service, including its cache and budget store.
            app.state.analysis_service = AnalysisService(config)
            # Import the optional adapter only for an enabled local Lab; share its budget store.
            if config.langchain_lab_enabled and not config.vercel:
                from app.services.langchain_analysis import LangChainAnalysisService

                # Give the Lab its own service while sharing the existing spending ledger.
                app.state.langchain_analysis_service = LangChainAnalysisService(
                    config, store=app.state.analysis_service.store
                )
            # Hand control back to FastAPI so it can serve requests until shutdown.
            yield
        finally:
            # Startup may fail partway through. Look up the service safely before attempting cleanup.
            service = getattr(app.state, "analysis_service", None)
            if service is not None:
                service.store.close()
            # await lets this function pause while MongoDB connections finish closing.
            await client.close()

    # Attach the startup/shutdown routine to the FastAPI application returned below.
    app = FastAPI(title="AI Product Review Intelligence Agent", lifespan=lifespan)
    # Browsers enforce CORS. These settings allow our UI origins to call the backend.
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
    # Register the health, normal analysis and LangChain routes collected in router.py.
    app.include_router(router)

    # Fail closed: without durable budget storage, spending cannot be safely authorized.
    # Register a handler that FastAPI calls when the matching exception escapes a route.
    @app.exception_handler(StorageUnavailableError)
    async def storage_unavailable(request: Request, exc: StorageUnavailableError) -> JSONResponse:
        # Return a small JSON error with a suitable HTTP status instead of exposing internal details.
        return JSONResponse(
            status_code=503,
            content={"detail": "Shared AI budget storage is unavailable. Please try again later."},
        )

    # Keep internal failures out of the public response.
    # Register a handler that FastAPI calls when the matching exception escapes a route.
    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error on %s", request.method, exc_info=exc)
        # Return a small JSON error with a suitable HTTP status instead of exposing internal details.
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


# Vercel discovers this ASGI object. Configuration and clients initialise at startup.
app = create_app()
