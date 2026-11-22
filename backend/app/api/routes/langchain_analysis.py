from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas.analysis import AnalysisRequest, AnalysisResponse

router = APIRouter()


# Backend access checks are independent of NEXT_PUBLIC_DEV_MODE, which only hides UI.
def require_local_lab(request: Request) -> None:
    # Read the same startup configuration used by the normal analysis service.
    config = request.app.state.analysis_service.config
    # Disabled means the experimental route should not be available, even if somebody calls it directly.
    if not config.langchain_lab_enabled:
        raise HTTPException(status_code=404, detail="LangChain Lab is disabled on this backend.")
    # This experimental endpoint is local-only, independently of all UI/environment flags.
    # Do not trust Origin, Host or forwarded headers as authentication.
    # Accept only direct loopback clients locally; a browser flag cannot grant backend access.
    if config.vercel or request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="LangChain Lab is restricted to local access.")


# Same request/report schemas as HTTP; dispatch to the separate LangChain service.
@router.post(
    "/analysis/langchain",
    response_model=AnalysisResponse,
    # Depends runs our access check before the route function can start analysis.
    dependencies=[Depends(require_local_lab)],
)
async def analyze_with_langchain(body: AnalysisRequest, request: Request) -> AnalysisResponse:
    # Call the separate LangChain service and return the same report format as the normal endpoint.
    return await request.app.state.langchain_analysis_service.analyze(body.url)
