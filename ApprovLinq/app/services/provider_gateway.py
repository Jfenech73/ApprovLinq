"""Provider gateway for scan-time page extraction.

Phase 7 keeps provider behaviour unchanged while giving the scan orchestrator a
small boundary around timeout handling and extractor invocation.
"""
from __future__ import annotations

import concurrent.futures as _cf

from app.config import settings
from app.services.extractor import process_pdf_page_rows


def process_page_rows_with_timeout(
    pdf_path: str,
    *,
    page_index: int,
    scan_mode: str,
    openai_api_key: str | None,
    account_company_name: str | None,
) -> list[dict]:
    """Run page extraction with the existing hard progress timeout."""
    timeout_s = float(getattr(settings, "extraction_page_timeout_s", 120) or 120)
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
        return future.result(timeout=timeout_s)
    except _cf.TimeoutError as exc:
        future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"Page extraction timed out after {timeout_s:.0f}s") from exc
    finally:
        if future.done():
            pool.shutdown(wait=False, cancel_futures=True)
