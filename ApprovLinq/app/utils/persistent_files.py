from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.utils.storage import resolve_upload_path

logger = logging.getLogger(__name__)


def attach_invoice_file_bytes(invoice_file: Any, content: bytes) -> None:
    """Persist uploaded PDF bytes in the database as the durable source of truth.

    Koyeb service filesystems are ephemeral unless an explicit persistent volume or
    external object storage is configured.  We still write a local runtime file for
    fast PDF processing, but the byte copy allows the app to re-materialise the PDF
    after a redeploy/restart.
    """
    try:
        invoice_file.file_bytes = bytes(content or b"")
        invoice_file.storage_backend = "database+local"
    except Exception:
        logger.warning("Could not attach durable bytes to invoice_file", exc_info=True)


def materialize_invoice_file(invoice_file: Any) -> Path:
    """Return a local path for an InvoiceFile, recreating it from DB bytes if lost.

    Existing code and PDF libraries expect a filesystem path.  This helper keeps
    that contract while making the stored DB bytes authoritative when the local
    Koyeb runtime cache has disappeared.
    """
    if invoice_file is None:
        raise FileNotFoundError("Invoice file record is missing")

    raw_path = getattr(invoice_file, "file_path", "") or ""
    path = resolve_upload_path(raw_path)
    if path.exists():
        return path

    data = getattr(invoice_file, "file_bytes", None)
    if not data:
        raise FileNotFoundError(
            f"PDF missing from local disk and no database file copy is available: {raw_path}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    logger.info("Re-materialised invoice PDF %s from database storage", path)
    return path
