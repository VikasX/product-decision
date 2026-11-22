from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas.analysis import AnalysisRequest, AnalysisResponse

router = APIRouter()


def require_local_lab(request: Request) -> None:
    config = request.app.state.analysis_service.config
    if not config.langchain_lab_enabled:
        raise HTTPException(status_code=404, detail="LangChain Lab is disabled on this backend.")
    # This experimental endpoint is local-only, independently of all UI/environment flags.
    # Do not trust Origin, Host or forwarded headers as authentication.
    if config.vercel or request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="LangChain Lab is restricted to local access.")


@router.post(
    "/analysis/langchain",
    response_model=AnalysisResponse,
    dependencies=[Depends(require_local_lab)],
)
async def analyze_with_langchain(body: AnalysisRequest, request: Request) -> AnalysisResponse:
    return await request.app.state.langchain_analysis_service.analyze(body.url)
