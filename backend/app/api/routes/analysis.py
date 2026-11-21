from fastapi import APIRouter, Request

from app.schemas.analysis import AnalysisRequest, AnalysisResponse

router = APIRouter()


@router.post("/analysis", response_model=AnalysisResponse)
async def analyze_product(body: AnalysisRequest, request: Request) -> AnalysisResponse:
    return await request.app.state.analysis_service.analyze(body.url)
