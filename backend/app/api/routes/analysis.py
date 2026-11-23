from fastapi import APIRouter, HTTPException, Request

from app.schemas.analysis import AnalysisRequest, AnalysisResponse

router = APIRouter()


def require_private_analysis(request: Request) -> None:
    # The public flag and Vercel marker independently prevent any remotely triggered paid work.
    config = request.app.state.config
    if config.public_saved_reports_only or config.vercel:
        raise HTTPException(status_code=404, detail="Not found")
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Analysis generation is local-only")


# Validated/canonical URL in; the service returns the same report contract used by both UIs.
# Map POST /api/v1/analysis to this function. FastAPI validates the returned report schema.
@router.post("/analysis", response_model=AnalysisResponse)
# FastAPI builds body from JSON using AnalysisRequest; request gives access to app-owned services.
async def analyze_product(body: AnalysisRequest, request: Request) -> AnalysisResponse:
    require_private_analysis(request)
    # Wait for the service report, then let FastAPI serialize it into the HTTP response.
    return await request.app.state.analysis_service.analyze(body.url)
