from fastapi import APIRouter

from app.api.routes import analysis, health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(analysis.router)
