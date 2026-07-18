"""Provider gateway for scan-time page extraction.

Phase 7 keeps provider behaviour unchanged while giving the scan orchestrator a
small boundary around timeout handling and extractor invocation.
"""
from __future__ import annotations

import concurrent.futures as _cf
from dataclasses import dataclass
import time

from app.config import settings
from app.services.extractor import process_pdf_page_rows


@dataclass(frozen=True)
class PageProviderResult:
    rows: list[dict]
    telemetry: dict


def _telemetry_from_rows(rows: list[dict], *, latency_ms: int, status: str = "success") -> dict:
    telemetry: dict = {
        "provider_name": "extractor",
        "provider_status": status,
        "latency_ms": int(latency_ms),
        "retries": 0,
    }
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_telemetry = row.get("_provider_telemetry")
        if isinstance(row_telemetry, dict):
            telemetry.update({k: v for k, v in row_telemetry.items() if v is not None})
        source = row.get("extraction_source") or row.get("method_used")
        if source and telemetry.get("provider_name") == "extractor":
            telemetry["provider_name"] = str(source).split("+")[0][:80]
    return telemetry


def process_page_rows_with_telemetry(
    pdf_path: str,
    *,
    page_index: int,
    scan_mode: str,
    openai_api_key: str | None,
    account_company_name: str | None,
) -> PageProviderResult:
    """Run page extraction and return rows plus persisted operational telemetry."""
    timeout_s = float(getattr(settings, "extraction_page_timeout_s", 120) or 120)
    started = time.perf_counter()
    pool = _cf.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        process_pdf_page_rows,
        pdf_path,
        page_index=page_index,
        scan_mode=scan_mode,
        openai_api_key=openai_api_key,
        account_company_name=account_company_name,
    )
    try:
        rows = future.result(timeout=timeout_s)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return PageProviderResult(rows=rows, telemetry=_telemetry_from_rows(rows, latency_ms=latency_ms))
    except _cf.TimeoutError as exc:
        future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry = {
            "provider_name": "extractor",
            "provider_status": "timeout",
            "latency_ms": latency_ms,
            "retries": 0,
            "timeout_reason": f"Page extraction timed out after {timeout_s:.0f}s",
        }
        timeout_error = TimeoutError(telemetry["timeout_reason"])
        setattr(timeout_error, "provider_telemetry", telemetry)
        raise timeout_error from exc
    finally:
        if future.done():
            pool.shutdown(wait=False, cancel_futures=True)


def process_page_rows_with_timeout(
    pdf_path: str,
    *,
    page_index: int,
    scan_mode: str,
    openai_api_key: str | None,
    account_company_name: str | None,
) -> list[dict]:
    """Run page extraction with the existing hard progress timeout."""
    return process_page_rows_with_telemetry(
        pdf_path,
        page_index=page_index,
        scan_mode=scan_mode,
        openai_api_key=openai_api_key,
        account_company_name=account_company_name,
    ).rows
