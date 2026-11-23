from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.routes.langchain_analysis import require_local_lab
from app.api.routes.analysis import require_private_analysis
from app.schemas.analysis import AnalysisResponse
from app.schemas.demo import DemoCategory, DemoDataset, DemoProduct
from app.services.analysis import TECHNICAL_FAILURE_PREFIX
from app.services.demo import DemoAnalysisService, DemoCatalog, Workflow
from app.services.saved_reports import SavedReportCatalog

# The parent router adds /api/v1, so these endpoints are served under /api/v1/demo.
router = APIRouter(prefix="/demo", tags=["demo"])
# This object knows how to read the manifest; construction itself does not fetch Amazon or call OpenAI.
catalog = DemoCatalog()
published = SavedReportCatalog()


def request_catalog(request: Request):
    # Catalog membership comes from the user's manifest, not from report/cache availability.
    # Adding saved-report delivery must never silently remove existing products or categories.
    return catalog


def dataset_or_404(product_id: str, source=catalog) -> DemoDataset:
    # Share product lookup between routes and convert an unknown ID into a safe client-facing error.
    try:
        return source.get(product_id)
    # Do not hide invalid dataset errors as 404: only an unknown product ID is a missing resource.
    except KeyError:
        raise HTTPException(status_code=404, detail="Saved product not found.") from None


# The decorator registers a GET route; response_model validates and serializes its returned category list.
@router.get("/categories", response_model=list[DemoCategory])
async def categories(request: Request):
    # Browsing category names/counts reads local JSON only and cannot spend AI credit.
    return request_catalog(request).categories()


# FastAPI takes category_id from the URL and passes it into this async handler.
@router.get("/categories/{category_id}/products", response_model=list[DemoProduct])
async def products(category_id: str, request: Request):
    # These lightweight records populate the frontend cards, not the full analysis dashboard.
    available = request_catalog(request).products(category_id)
    for item in available:
        try:
            published.report_for(catalog.get(item.id))
            item.analysis_ready = True
        except (OSError, ValueError):
            # Readers can still inspect the saved product evidence without generating an AI report.
            pass
    # Treat an unknown or empty category as unavailable rather than inventing placeholder products.
    if not available:
        raise HTTPException(status_code=404, detail="Saved category not found.")
    return available


# Product selection loads the saved evidence first; it still does not start analysis.
@router.get("/products/{product_id}", response_model=DemoDataset)
async def dataset(product_id: str, request: Request):
    return dataset_or_404(product_id, request_catalog(request))


# Serve the local product photo through the API so browsing does not depend on Amazon or raw screenshots.
@router.get("/products/{product_id}/image")
async def image(product_id: str, request: Request):
    saved = dataset_or_404(product_id, request_catalog(request))
    # Missing optional images get a clear 404 rather than an attempt to open an invalid filename.
    if not saved.image_file:
        raise HTTPException(status_code=404, detail="Product image unavailable.")
    # FileResponse infers JPEG/PNG from the filename; safe_path keeps the asset inside product/.
    return FileResponse(catalog.safe_path(saved.image_file))


@router.get("/products/{product_id}/analysis", response_model=AnalysisResponse)
async def saved_analysis(product_id: str):
    # Always read the permanent file. There is deliberately no call to either LLM service here.
    saved = dataset_or_404(product_id, published)
    return published.report_for(saved)


# POST marks the explicit analysis action. workflow is a validated query parameter, defaulting to HTTPX.
@router.post("/products/{product_id}/analysis", response_model=AnalysisResponse)
async def analyze(product_id: str, request: Request, workflow: Workflow = "httpx"):
    # Old deployed clients may still POST. In public mode this is a read-only compatibility path.
    if request.app.state.config.saved_reports_mode:
        return await saved_analysis(product_id)
    require_private_analysis(request)
    # The developer UI flag is not an access control. Keep the existing local Lab policy.
    if workflow == "langchain":
        require_local_lab(request)
    # Read the chosen product's validated JSON; Demo Mode never invokes the Amazon collector.
    saved = dataset_or_404(product_id)
    # Startup created this service once. Reuse its settings and ledger so requests cannot reset their budget.
    original = request.app.state.analysis_service
    service = DemoAnalysisService(
        original.config, store=original.store, dataset=saved, workflow=workflow
    )
    # The inherited flow checks cache, takes a lease, reserves cost, calls the chosen adapter and saves the report.
    result = await service.analyze(saved.product.url)
    # The service already cached the failure for 60 seconds. Return an error rather than
    # displaying a technical provider/parser problem as a product verdict in the demo UI.
    if any(line.startswith(TECHNICAL_FAILURE_PREFIX) for line in result.coverage.limitations):
        raise HTTPException(status_code=502, detail=result.summary)
    # A budget/configuration guard is different from invalid model output: it prevented the model call entirely.
    if "No new AI request was made." in result.coverage.limitations:
        raise HTTPException(status_code=503, detail=result.summary)
    # Return the same report contract used by the original HTTP and LangChain report components.
    return result
