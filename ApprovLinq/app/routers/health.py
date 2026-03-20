from fastapi import APIRouter
from app.config import settings
from app.services.extractor import azure_di_available

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    di_ok, di_reason = azure_di_available()

    engines = {
        "openai_vision": {
            "enabled": settings.use_openai,
            "model": settings.openai_model if settings.use_openai else None,
            "status": "ok" if settings.use_openai and settings.openai_api_key else "not_configured",
        },
        "azure_document_intelligence": {
            "enabled": settings.use_azure_di,
            "endpoint": settings.azure_di_endpoint if settings.use_azure_di else None,
            "status": "ok" if di_ok else ("error" if settings.use_azure_di else "disabled"),
            "error": di_reason if not di_ok and settings.use_azure_di else None,
        },
    }

    active_engine = "azure_di" if di_ok else ("openai_vision" if settings.use_openai else "rule_based_only")

    return {
        "status": "ok",
        "active_extraction_engine": active_engine,
        "engines": engines,
    }
