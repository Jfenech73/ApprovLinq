"""
app/services/preflight.py
─────────────────────────
Azure DI configuration check before batch processing starts.

Change from previous version
─────────────────────────────
The previous implementation performed a standalone network readiness ping
(HTTP GET /documentModels) before any real extraction.  When that ping
returned HTTP 404 — which can happen legitimately with certain Azure DI
endpoint configurations while real extraction still works — the batch was
incorrectly forced onto the fallback path and a misleading
a misleading readiness-check-failed error
message was emitted.

This version removes the network preflight entirely.  The actual Azure DI
extraction call in extractor.py is the authoritative source of truth for
availability.  The per-page circuit-breaker already handles real failures
precisely and logs accurate HTTP status codes from the real extraction
response.

States
──────
  AZURE_DI_DISABLED    — USE_AZURE_DI flag is off.
  AZURE_DI_CONFIGURED  — Flag on and credentials/endpoint present.
                         Azure DI will be attempted; the per-page
                         circuit-breaker in extractor.py catches real
                         failures and falls back accordingly.
  AZURE_DI_NOT_READY   — Config-level problem only (missing/invalid
                         endpoint or key) — Azure DI cannot be attempted
                         without valid credentials.

Decision rule
─────────────
  AZURE_DI_CONFIGURED → ExtractionBackend.AZURE_DI
  AZURE_DI_NOT_READY  → ExtractionBackend.FALLBACK_NON_AZURE
  AZURE_DI_DISABLED   → ExtractionBackend.FALLBACK_NON_AZURE

Error handling (real extraction, in extractor.py)
──────────────────────────────────────────────────
When the real Azure DI extraction call fails the extractor logs a precise
reason based on the actual response status:
  401/403 → authentication / authorisation / VNet denial
  404     → endpoint or model route not found
  429     → throttling / rate-limiting
  5xx     → Azure service-side failure
  timeout → connectivity / poller timeout

No network ping is performed here.  Never logs secrets.
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
    """Configuration-level Azure DI check outcome (no network ping)."""
    DISABLED    = "disabled"       # USE_AZURE_DI=false
    CONFIGURED  = "configured"     # credentials present; DI will be attempted
    NOT_READY   = "not_ready"      # config-level problem (missing/invalid creds)


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
    readiness_ok:       Optional[bool]   # Always None — no network check
    failure_reason:     Optional[str]    # human-readable on config failure
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


def _check_endpoint_format(endpoint: str | None) -> bool:
    """Return True if the endpoint looks like a valid HTTPS URL."""
    if not endpoint or not endpoint.strip():
        return False
    ep = endpoint.strip()
    if not ep.lower().startswith("https://"):
        return False
    return bool(_ENDPOINT_RE.match(ep))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_preflight_checks(
    *,
    skip_readiness_check: bool = False,   # kept for API compatibility; ignored
) -> PreflightResult:
    """Check Azure DI configuration and return an ExtractionBackend decision.

    No network request is made.  The actual extraction call in extractor.py
    is the authoritative source of truth for Azure DI availability; its
    per-page circuit-breaker handles real failures with accurate error messages.

    Parameters
    ──────────
    skip_readiness_check : bool
        Retained for backwards-compatibility with callers.  Ignored — the
        network readiness check has been removed entirely.

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
    readiness_ok: Optional[bool] = None   # no network check; always None
    failure_reason: Optional[str] = None
    readiness_state: AzureDiReadinessState

    logger.info(
        "preflight: started — azure_di_enabled=%s endpoint_present=%s "
        "key_present=%s endpoint_valid=%s",
        azure_di_enabled, endpoint_present, key_present, endpoint_valid,
    )

    # ── Config-level checks only (no network) ───────────────────────────────
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

    else:
        # Credentials present and format valid.  Attempt Azure DI extraction.
        # Real availability is determined by the first extraction call; any
        # failure there will open the circuit-breaker with a precise reason.
        readiness_state = AzureDiReadinessState.CONFIGURED
        logger.info(
            "preflight: Azure DI configured — will attempt extraction directly "
            "(no network preflight; per-page circuit-breaker handles real failures)"
        )

    # ── Backend selection ────────────────────────────────────────────────────
    if readiness_state == AzureDiReadinessState.CONFIGURED:
        selected = ExtractionBackend.AZURE_DI
        notes = (
            "Preflight: Azure DI configured — attempting Azure Document Intelligence extraction"
        )
        logger.info("preflight: selected backend=azure_di (state=configured)")
    else:
        selected = ExtractionBackend.FALLBACK_NON_AZURE
        if readiness_state == AzureDiReadinessState.DISABLED:
            notes = "Preflight: Azure DI disabled — using fallback extraction"
        else:
            notes = f"Preflight: Azure DI not configured — using fallback extraction ({failure_reason})"
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
