"""
app/services/preflight.py
─────────────────────────
Lightweight pre-processing health check that decides which extraction backend
to use before a batch or file is processed.

Goals:
  • Run once per batch, not once per page.
  • Finish in < 2 s on a healthy system (no actual document is processed).
  • Return a clear ExtractionBackend decision so the batch job never enters a
    looping/retrying Azure DI failure path mid-scan.
  • Never log secrets (keys are presence-checked only).

Decision values
───────────────
  "azure_di"           — Azure DI is enabled, credentials present, endpoint
                         format looks valid, and an optional lightweight
                         connectivity ping passed (or ping was skipped).
  "fallback_non_azure" — Azure DI is disabled, misconfigured, or the
                         connectivity check failed.  The non-Azure pipeline
                         (OpenAI vision / OpenAI text / rule-based) is used.

Usage (from _process_batch_job in batches.py)
─────────────────────────────────────────────
    from app.services.preflight import run_preflight_checks, ExtractionBackend

    result = run_preflight_checks()
    # Write result.notes to batch.notes and log result.selected_backend
    if result.selected_backend == ExtractionBackend.AZURE_DI:
        openai_api_key = settings.openai_api_key if settings.use_openai else None
        # Azure DI is used inside process_pdf_page via azure_di_available()
    else:
        # Force-disable Azure DI for this batch by resetting the circuit
        # breaker flag only when the selection is already "fallback", so the
        # extractor's azure_di_available() returns False and skips DI attempts.
        from app.services.extractor import _reset_azure_di_error
        # (no-op if already disabled by config)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class ExtractionBackend(str, Enum):
    AZURE_DI           = "azure_di"
    FALLBACK_NON_AZURE = "fallback_non_azure"


@dataclass
class PreflightResult:
    """Outcome of a single preflight check run."""
    selected_backend: ExtractionBackend
    azure_di_enabled: bool
    endpoint_present: bool
    key_present:      bool
    endpoint_valid:   bool
    connectivity_ok:  Optional[bool]   # None = skipped
    failure_reason:   Optional[str]    # human-readable if backend != azure_di
    # Ready-to-use note for batch.notes  (no secrets)
    notes:            str = field(default="")
    duration_ms:      int = field(default=0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ENDPOINT_RE = re.compile(
    r"^https://[a-zA-Z0-9][a-zA-Z0-9\-]{1,61}[a-zA-Z0-9]"
    r"(?:\.[a-zA-Z0-9][a-zA-Z0-9\-]*)*"
    r"(?:/[^\s]*)?$"
)


def _check_endpoint_format(endpoint: str | None) -> bool:
    """Return True if the endpoint looks like a valid HTTPS URL."""
    if not endpoint or not endpoint.strip():
        return False
    ep = endpoint.strip()
    if not ep.lower().startswith("https://"):
        return False
    return bool(_ENDPOINT_RE.match(ep))


def _connectivity_ping(endpoint: str, key: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Try a HEAD request against the Azure DI endpoint (no document sent).

    Uses the /documentModels endpoint — a cheap metadata list call that
    requires a valid key and reachable endpoint.  The response body is not
    read.  We only care about HTTP 2xx vs 4xx/5xx and network errors.

    Returns (success, reason_on_failure).
    Never raises.
    """
    try:
        import urllib.request
        import urllib.error
        url = endpoint.rstrip("/") + "/documentModels?api-version=2024-11-30"
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("Ocp-Apim-Subscription-Key", key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        # 401/403 = auth failure (real endpoint, wrong key/VNet policy)
        # 200/404/405 = reachable (404 = path wrong but server replied)
        if status in (401, 403):
            return False, f"Azure DI auth failed (HTTP {status})"
        # 404 or 405 on a HEAD is fine — the endpoint is reachable
        if status in (404, 405, 200):
            return True, ""
        return False, f"Azure DI connectivity check returned HTTP {status}"
    except Exception as exc:
        return False, f"Azure DI connectivity check failed: {exc!s:.120}"

    if 200 <= status < 300:
        return True, ""
    return False, f"Azure DI connectivity check returned HTTP {status}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_preflight_checks(
    *,
    skip_connectivity_ping: bool = False,
) -> PreflightResult:
    """Run lightweight pre-processing health checks.

    Parameters
    ──────────
    skip_connectivity_ping : bool
        When True the network ping is skipped (useful in unit tests or when
        the caller knows the network is not reachable).  Config-validation
        checks still run.

    Returns
    ───────
    PreflightResult with a .selected_backend and human-readable .notes.
    """
    t0 = time.monotonic()

    azure_di_enabled  = bool(getattr(settings, "use_azure_di", False))
    endpoint          = (getattr(settings, "azure_di_endpoint", None) or "").strip()
    key               = (getattr(settings, "azure_di_key",      None) or "").strip()
    endpoint_present  = bool(endpoint)
    key_present       = bool(key)
    endpoint_valid    = _check_endpoint_format(endpoint) if endpoint_present else False
    connectivity_ok: Optional[bool]  = None
    failure_reason: Optional[str]    = None

    logger.debug(
        "preflight: azure_di_enabled=%s endpoint_present=%s key_present=%s endpoint_valid=%s",
        azure_di_enabled, endpoint_present, key_present, endpoint_valid,
    )

    # ── Decision tree ───────────────────────────────────────────────────────
    if not azure_di_enabled:
        failure_reason = "USE_AZURE_DI is disabled in configuration"
        logger.debug("preflight: %s → fallback", failure_reason)

    elif not endpoint_present:
        failure_reason = "AZURE_DI_ENDPOINT is not configured"
        logger.warning("preflight: %s → fallback", failure_reason)

    elif not key_present:
        failure_reason = "AZURE_DI_KEY is not configured"
        logger.warning("preflight: %s → fallback", failure_reason)

    elif not endpoint_valid:
        failure_reason = (
            f"AZURE_DI_ENDPOINT does not look like a valid HTTPS URL "
            f"(value starts with: {endpoint[:40]!r})"
        )
        logger.warning("preflight: %s → fallback", failure_reason)

    else:
        # Config looks valid — optionally verify connectivity
        if not skip_connectivity_ping:
            ping_ok, ping_reason = _connectivity_ping(endpoint, key)
            connectivity_ok = ping_ok
            if not ping_ok:
                failure_reason = ping_reason
                logger.warning(
                    "preflight: connectivity ping failed — %s → fallback", ping_reason
                )
            else:
                logger.debug("preflight: connectivity ping OK")
        else:
            logger.debug("preflight: connectivity ping skipped")

    # ── Select backend ───────────────────────────────────────────────────────
    if failure_reason is None:
        selected = ExtractionBackend.AZURE_DI
        notes = (
            "Preflight: Azure DI available — using Azure Document Intelligence extraction"
        )
        logger.info("preflight: selected backend=azure_di")
    else:
        selected = ExtractionBackend.FALLBACK_NON_AZURE
        notes = f"Preflight: Azure DI unavailable ({failure_reason}) — using fallback extraction"
        logger.info("preflight: selected backend=fallback_non_azure reason=%r", failure_reason)

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.debug("preflight completed in %d ms", duration_ms)

    return PreflightResult(
        selected_backend  = selected,
        azure_di_enabled  = azure_di_enabled,
        endpoint_present  = endpoint_present,
        key_present       = key_present,
        endpoint_valid    = endpoint_valid,
        connectivity_ok   = connectivity_ok,
        failure_reason    = failure_reason,
        notes             = notes,
        duration_ms       = duration_ms,
    )
