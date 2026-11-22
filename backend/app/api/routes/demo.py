from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.routes.langchain_analysis import require_local_lab
from app.schemas.analysis import AnalysisResponse
from app.schemas.demo import DemoCategory, DemoDataset, DemoProduct
from app.services.demo import DemoAnalysisService, DemoCatalog, Workflow

router = APIRouter(prefix="/demo", tags=["demo"])
catalog = DemoCatalog()


def dataset_or_404(product_id: str) -> DemoDataset:
    try:
        return catalog.get(product_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Saved product not found.") from None


@router.get("/categories", response_model=list[DemoCategory])
async def categories():
    return catalog.categories()


@router.get("/categories/{category_id}/products", response_model=list[DemoProduct])
async def products(category_id: str):
    available = catalog.products(category_id)
    if not available:
        raise HTTPException(status_code=404, detail="Saved category not found.")
    return available


@router.get("/products/{product_id}", response_model=DemoDataset)
async def dataset(product_id: str):
    return dataset_or_404(product_id)


@router.get("/products/{product_id}/image")
async def image(product_id: str):
    saved = dataset_or_404(product_id)
    if not saved.image_file:
        raise HTTPException(status_code=404, detail="Product image unavailable.")
    return FileResponse(catalog.safe_path(saved.image_file), media_type="image/png")


@router.post("/products/{product_id}/analysis", response_model=AnalysisResponse)
async def analyze(product_id: str, request: Request, workflow: Workflow = "httpx"):
    # The developer UI flag is not an access control. Keep the existing local Lab policy.
    if workflow == "langchain":
        require_local_lab(request)
    saved = dataset_or_404(product_id)
    original = request.app.state.analysis_service
    service = DemoAnalysisService(
        original.config, store=original.store, dataset=saved, workflow=workflow
    )
    return await service.analyze(saved.product.url)
