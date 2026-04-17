"""
app/services/preflight.py
─────────────────────────
Real Azure DI readiness check before batch processing starts.

Problem with the previous implementation
──────────────────────────────────────────
The previous preflight only verified that USE_AZURE_DI=true, an endpoint was
present, and a key was present.  It skipped the connectivity ping by default,
so it always reported "Azure DI available" even when Azure DI was unreachable
or the key was invalid.  The note was therefore misleading.

This version
────────────
Introduces four explicit states:

  AZURE_DI_DISABLED     — USE_AZURE_DI flag is off.  Fallback used.
  AZURE_DI_CONFIGURED   — Flag on and credentials present but readiness
                          check was deliberately skipped (test / offline mode).
                          Azure DI path is used with the existing per-page
                          circuit-breaker as the safety net.
  AZURE_DI_READY        — Readiness check passed: a real authenticated GET
                          against /documentModels returned HTTP 2xx.
                          Azure DI path is used.
  AZURE_DI_NOT_READY    — Readiness check ran but failed (auth error, timeout,
                          DNS failure, unexpected HTTP status).
                          Fallback path is used immediately.

Decision rule
─────────────
  AZURE_DI_READY      → ExtractionBackend.AZURE_DI
  AZURE_DI_CONFIGURED → ExtractionBackend.AZURE_DI   (circuit-breaker catches failures)
  AZURE_DI_NOT_READY  → ExtractionBackend.FALLBACK_NON_AZURE
  AZURE_DI_DISABLED   → ExtractionBackend.FALLBACK_NON_AZURE

Readiness check details
───────────────────────
• HTTP GET  /documentModels?api-version=2024-11-30
  (not HEAD — Azure DI returns 405 on HEAD, which would be a false positive)
• Ocp-Apim-Subscription-Key: <key>  header
• Timeout: 5 s (configurable via AZURE_DI_PREFLIGHT_TIMEOUT env var)
• Reads only the status code; response body is discarded immediately
• 200 → ready
• 401 / 403 → auth failure → NOT_READY
• Other 4xx/5xx → NOT_READY
• Network / timeout → NOT_READY

Per-page timeout safeguard (in extractor.py)
────────────────────────────────────────────
azure_di_extract_invoice() wraps poller.result() with a thread-based timeout
(AZURE_DI_PAGE_TIMEOUT_S, default 45 s).  If the poller does not complete
within that window the call raises TimeoutError, which triggers the existing
fallback chain (OpenAI vision → OpenAI text → rule-based).

Never logs secrets.
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

class AzureDiReadinessState(str, Enum):
    """Fine-grained Azure DI preflight outcome."""
    DISABLED    = "disabled"       # USE_AZURE_DI=false
    CONFIGURED  = "configured"     # credentials present; ping skipped
    READY       = "ready"          # authenticated GET returned 200
    NOT_READY   = "not_ready"      # check ran but failed


class ExtractionBackend(str, Enum):
    """Coarse backend decision used by the batch job."""
    AZURE_DI           = "azure_di"
    FALLBACK_NON_AZURE = "fallback_non_azure"


@dataclass
class PreflightResult:
    """Outcome of a single preflight check run."""
    selected_backend:   ExtractionBackend
    readiness_state:    AzureDiReadinessState
    azure_di_enabled:   bool
    endpoint_present:   bool
    key_present:        bool
    endpoint_valid:     bool
    readiness_ok:       Optional[bool]   # None = check was skipped
    failure_reason:     Optional[str]    # human-readable on failure
    notes:              str  = field(default="")
    duration_ms:        int  = field(default=0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ENDPOINT_RE = re.compile(
    r"^https://[a-zA-Z0-9][a-zA-Z0-9\-]{1,61}[a-zA-Z0-9]"
    r"(?:\.[a-zA-Z0-9][a-zA-Z0-9\-]*)*"
    r"(?:/[^\s]*)?$"
)

# Default per-call timeout for the readiness GET request (seconds).
_READINESS_TIMEOUT_DEFAULT = 5.0


def _check_endpoint_format(endpoint: str | None) -> bool:
    """Return True if the endpoint looks like a valid HTTPS URL."""
    if not endpoint or not endpoint.strip():
        return False
    ep = endpoint.strip()
    if not ep.lower().startswith("https://"):
        return False
    return bool(_ENDPOINT_RE.match(ep))


def _readiness_check(
    endpoint: str,
    key: str,
    timeout: float = _READINESS_TIMEOUT_DEFAULT,
) -> tuple[bool, str]:
    """Perform a real authenticated GET against Azure DI /documentModels.

    Uses GET (not HEAD) because Azure DI returns 405 on HEAD requests,
    which would be a false-positive success on a misconfigured endpoint.

    Returns (True, "") on success, (False, reason) on any failure.
    Never raises.  Never logs the key value.
    """
    try:
        import urllib.request
        import urllib.error

        url = endpoint.rstrip("/") + "/documentModels?api-version=2024-11-30"
        logger.debug("preflight: readiness GET %s (timeout=%.1fs)", url, timeout)

        req = urllib.request.Request(url, method="GET")
        req.add_header("Ocp-Apim-Subscription-Key", key)
        # Read and discard the body immediately; we only need the status code.
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _ = resp.read(64)   # consume minimal bytes so the socket closes cleanly
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code

        logger.debug("preflight: readiness check HTTP status=%d", status)

        if status == 200:
            return True, ""
        if status in (401, 403):
            return False, f"Azure DI authentication failed (HTTP {status}) — check AZURE_DI_KEY and VNet rules"
        if status == 404:
            return False, f"Azure DI endpoint not found (HTTP 404) — check AZURE_DI_ENDPOINT value"
        return False, f"Azure DI readiness check returned unexpected HTTP {status}"

    except TimeoutError:
        return False, f"Azure DI readiness check timed out after {timeout:.0f}s"
    except OSError as exc:
        # socket.gaierror (DNS), ConnectionRefusedError, etc.
        return False, f"Azure DI readiness check network error: {exc!s:.120}"
    except Exception as exc:
        return False, f"Azure DI readiness check failed: {exc!s:.120}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_preflight_checks(
    *,
    skip_readiness_check: bool = False,
) -> PreflightResult:
    """Run the Azure DI preflight and return an ExtractionBackend decision.

    Parameters
    ──────────
    skip_readiness_check : bool
        When True the network readiness check is skipped and the state becomes
        AZURE_DI_CONFIGURED rather than AZURE_DI_READY/NOT_READY.
        Use in unit tests or when operating in a network-restricted environment
        where the per-page circuit-breaker is the fallback safety net.

    Returns
    ───────
    PreflightResult with .selected_backend and human-readable .notes.
    The .notes string is safe to write directly to batch.notes (no secrets).
    """
    t0 = time.monotonic()

    azure_di_enabled = bool(getattr(settings, "use_azure_di", False))
    endpoint         = (getattr(settings, "azure_di_endpoint", None) or "").strip()
    key              = (getattr(settings, "azure_di_key",      None) or "").strip()
    endpoint_present = bool(endpoint)
    key_present      = bool(key)
    endpoint_valid   = _check_endpoint_format(endpoint) if endpoint_present else False
    readiness_ok: Optional[bool] = None
    failure_reason: Optional[str] = None
    readiness_state: AzureDiReadinessState

    logger.info(
        "preflight: started — azure_di_enabled=%s endpoint_present=%s "
        "key_present=%s endpoint_valid=%s",
        azure_di_enabled, endpoint_present, key_present, endpoint_valid,
    )

    # ── Config-level checks (no network) ────────────────────────────────────
    if not azure_di_enabled:
        readiness_state = AzureDiReadinessState.DISABLED
        failure_reason  = "USE_AZURE_DI is disabled in configuration"
        logger.info("preflight: Azure DI disabled → fallback")

    elif not endpoint_present:
        readiness_state = AzureDiReadinessState.NOT_READY
        failure_reason  = "AZURE_DI_ENDPOINT is not configured"
        logger.warning("preflight: %s → fallback", failure_reason)

    elif not key_present:
        readiness_state = AzureDiReadinessState.NOT_READY
        failure_reason  = "AZURE_DI_KEY is not configured"
        logger.warning("preflight: %s → fallback", failure_reason)

    elif not endpoint_valid:
        readiness_state = AzureDiReadinessState.NOT_READY
        failure_reason  = (
            f"AZURE_DI_ENDPOINT is not a valid HTTPS URL "
            f"(starts with: {endpoint[:40]!r})"
        )
        logger.warning("preflight: %s → fallback", failure_reason)

    elif skip_readiness_check:
        # Credentials present and format valid but network check deliberately skipped.
        # Report CONFIGURED — not READY — so the note is honest.
        readiness_state = AzureDiReadinessState.CONFIGURED
        logger.info(
            "preflight: Azure DI configured (readiness check skipped) — "
            "using Azure DI with per-page circuit-breaker as safety net"
        )

    else:
        # ── Real network readiness check ─────────────────────────────────────
        # Get timeout from environment/config if set (default 5 s).
        _timeout = float(getattr(settings, "azure_di_preflight_timeout", _READINESS_TIMEOUT_DEFAULT))
        logger.info("preflight: running Azure DI readiness check (timeout=%.1fs)", _timeout)

        ok, reason = _readiness_check(endpoint, key, timeout=_timeout)
        readiness_ok = ok

        if ok:
            readiness_state = AzureDiReadinessState.READY
            logger.info("preflight: Azure DI readiness check PASSED → azure_di backend selected")
        else:
            readiness_state = AzureDiReadinessState.NOT_READY
            failure_reason  = reason
            logger.warning(
                "preflight: Azure DI readiness check FAILED — %s → fallback", reason
            )

    # ── Backend selection ────────────────────────────────────────────────────
    if readiness_state in (AzureDiReadinessState.READY, AzureDiReadinessState.CONFIGURED):
        selected = ExtractionBackend.AZURE_DI
        if readiness_state == AzureDiReadinessState.READY:
            notes = "Preflight: Azure DI ready — using Azure Document Intelligence extraction"
        else:
            notes = (
                "Preflight: Azure DI configured (readiness unverified) — "
                "using Azure DI extraction with circuit-breaker fallback"
            )
        logger.info("preflight: selected backend=azure_di (state=%s)", readiness_state.value)
    else:
        selected = ExtractionBackend.FALLBACK_NON_AZURE
        if readiness_state == AzureDiReadinessState.DISABLED:
            notes = "Preflight: Azure DI disabled — using fallback extraction"
        else:
            notes = f"Preflight: Azure DI not ready — using fallback extraction ({failure_reason})"
        logger.info(
            "preflight: selected backend=fallback_non_azure (state=%s reason=%r)",
            readiness_state.value, failure_reason,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info("preflight: completed in %d ms", duration_ms)

    return PreflightResult(
        selected_backend = selected,
        readiness_state  = readiness_state,
        azure_di_enabled = azure_di_enabled,
        endpoint_present = endpoint_present,
        key_present      = key_present,
        endpoint_valid   = endpoint_valid,
        readiness_ok     = readiness_ok,
        failure_reason   = failure_reason,
        notes            = notes,
        duration_ms      = duration_ms,
    )
