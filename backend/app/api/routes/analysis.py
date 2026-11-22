from fastapi import APIRouter, Request

from app.schemas.analysis import AnalysisRequest, AnalysisResponse

router = APIRouter()


# Validated/canonical URL in; the service returns the same report contract used by both UIs.
# Map POST /api/v1/analysis to this function. FastAPI validates the returned report schema.
@router.post("/analysis", response_model=AnalysisResponse)
# FastAPI builds body from JSON using AnalysisRequest; request gives access to app-owned services.
async def analyze_product(body: AnalysisRequest, request: Request) -> AnalysisResponse:
    # Wait for the service report, then let FastAPI serialize it into the HTTP response.
    return await request.app.state.analysis_service.analyze(body.url)
