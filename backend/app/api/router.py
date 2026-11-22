from fastapi import APIRouter

from app.api.routes import analysis, health, langchain_analysis

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(analysis.router)
router.include_router(langchain_analysis.router)
