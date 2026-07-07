from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import InvoiceBatch, ScanRun


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _app_version() -> str | None:
    try:
        version_file = Path(__file__).resolve().parents[2] / "VERSION"
        return version_file.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _extractor_build_tag() -> str | None:
    try:
        from app.services.extractor import EXTRACTOR_BUILD_TAG

        return str(EXTRACTOR_BUILD_TAG)
    except Exception:
        return None


def settings_fingerprint() -> str:
    return _stable_hash({
        "scan_mode_defaults": {
            "scan_provider_baseline_mode": bool(getattr(settings, "scan_provider_baseline_mode", False)),
            "normalize_page_orientation": bool(getattr(settings, "normalize_page_orientation", True)),
            "local_orientation_enabled": bool(getattr(settings, "local_orientation_enabled", True)),
            "azure_di_read_text_fallback": bool(getattr(settings, "azure_di_read_text_fallback", False)),
        },
        "timeouts": {
            "azure_di_page_timeout_s": getattr(settings, "azure_di_page_timeout_s", None),
            "extraction_page_timeout_s": getattr(settings, "extraction_page_timeout_s", None),
            "extraction_consecutive_timeout_limit": getattr(settings, "extraction_consecutive_timeout_limit", None),
        },
    })


def provider_config_fingerprint() -> str:
    endpoint = str(getattr(settings, "azure_di_endpoint", "") or "").strip().lower()
    endpoint_host = endpoint.split("//", 1)[-1].split("/", 1)[0] if endpoint else ""
    return _stable_hash({
        "azure_di": {
            "enabled": bool(getattr(settings, "use_azure_di", False)),
            "endpoint_host": endpoint_host,
            "key_present": bool(getattr(settings, "azure_di_key", None)),
        },
        "openai": {
            "enabled": bool(getattr(settings, "use_openai", False)),
            "model": getattr(settings, "openai_model", None),
            "key_present": bool(getattr(settings, "openai_api_key", None)),
        },
        "ocr": {
            "provider": getattr(settings, "ocr_provider", None),
            "ocr_space_endpoint": getattr(settings, "ocr_space_endpoint", None),
            "ocr_space_key_present": bool(getattr(settings, "ocr_space_api_key", None)),
            "ocr_space_language": getattr(settings, "ocr_space_language", None),
            "ocr_space_engine": getattr(settings, "ocr_space_ocr_engine", None),
            "paddle_enabled": bool(getattr(settings, "enable_paddle_ocr", False)),
        },
    })


def create_scan_run(db: Session, batch: InvoiceBatch) -> ScanRun:
    """Create a new immutable scan run and make it the current batch projection."""
    current_run_id = getattr(batch, "current_scan_run_id", None)
    max_run_number = (
        db.query(func.max(ScanRun.run_number))
        .filter(ScanRun.batch_id == batch.id)
        .scalar()
        or 0
    )
    run = ScanRun(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        run_number=int(max_run_number) + 1,
        parent_run_id=current_run_id,
        status="processing",
        app_version=_app_version(),
        extractor_build_tag=_extractor_build_tag(),
        scan_mode=batch.scan_mode or "summary",
        settings_fingerprint=settings_fingerprint(),
        provider_config_fingerprint=provider_config_fingerprint(),
        started_at=_utcnow(),
    )
    db.add(run)
    db.flush()
    batch.current_scan_run_id = run.id
    return run


def mark_scan_run_completed(
    db: Session,
    run: ScanRun,
    *,
    status: str,
    selected_backend: str | None = None,
    page_count: int | None = None,
    row_count: int | None = None,
    notes: str | None = None,
) -> None:
    run.status = status
    run.selected_backend = selected_backend
    run.page_count = page_count
    run.row_count = row_count
    run.notes = notes
    run.completed_at = _utcnow()


def mark_scan_run_failed(db: Session, run: ScanRun, *, error_message: str, notes: str | None = None) -> None:
    run.status = "failed"
    run.error_message = error_message[:2000]
    run.notes = notes
    run.completed_at = _utcnow()


def current_scan_run_id(batch: InvoiceBatch) -> Any:
    return getattr(batch, "current_scan_run_id", None)
