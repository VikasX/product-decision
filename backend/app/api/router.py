from fastapi import APIRouter

from app.api.routes import analysis, demo, health, langchain_analysis

# Group endpoint modules under the API prefix used by the frontend.
router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(analysis.router)
router.include_router(demo.router)
router.include_router(langchain_analysis.router)
