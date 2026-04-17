"""
Regression tests for the Azure DI preflight check.

Tests cover:
  1. Azure DI disabled  → fallback_non_azure immediately, no ping attempted
  2. Endpoint missing   → fallback_non_azure with clear reason
  3. Key missing        → fallback_non_azure with clear reason
  4. Endpoint bad URL   → fallback_non_azure without a network call
  5. All config present → azure_di selected (ping skipped in unit tests)
  6. Ping fails         → fallback_non_azure with connectivity reason
  7. Result notes never contain the secret key
  8. _reset_azure_di_error clears circuit breaker
  9. Batch job has preflight integration point
 10. BCRS / split / remap logic untouched

Run: pytest tests/test_preflight.py -v
"""
from __future__ import annotations

import ast
import os
import sys
import types
import unittest.mock as mock


# ── helpers ────────────────────────────────────────────────────────────────

def _src(f: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()


def _make_settings(**kwargs):
    """Return a minimal settings-like object for preflight tests."""
    defaults = dict(
        use_azure_di=False,
        azure_di_endpoint=None,
        azure_di_key=None,
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _run_preflight(settings_obj, skip_connectivity_ping=True):
    """Import and run preflight with a mocked settings object."""
    import importlib
    import app.services.preflight as pf_module
    with mock.patch.object(pf_module, "settings", settings_obj):
        return pf_module.run_preflight_checks(
            skip_connectivity_ping=skip_connectivity_ping,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Azure DI disabled
# ══════════════════════════════════════════════════════════════════════════════

class TestAzureDiDisabled:
    def test_fallback_when_disabled(self):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=False,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="fake-key-123",
        ))
        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_azure_di_enabled_flag_false(self):
        result = _run_preflight(_make_settings(use_azure_di=False))
        assert result.azure_di_enabled is False

    def test_no_ping_when_disabled(self):
        """When Azure DI is disabled, connectivity_ok must be None (no ping)."""
        result = _run_preflight(
            _make_settings(use_azure_di=False,
                           azure_di_endpoint="https://example.cognitiveservices.azure.com",
                           azure_di_key="some-key"),
            skip_connectivity_ping=False,  # even if not skipped, disabled = no ping
        )
        assert result.connectivity_ok is None


# ══════════════════════════════════════════════════════════════════════════════
# 2.  Endpoint missing
# ══════════════════════════════════════════════════════════════════════════════

class TestEndpointMissing:
    def test_fallback_when_endpoint_none(self):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=True, azure_di_endpoint=None, azure_di_key="somekey",
        ))
        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_fallback_when_endpoint_blank(self):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=True, azure_di_endpoint="   ", azure_di_key="somekey",
        ))
        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_endpoint_present_false(self):
        result = _run_preflight(_make_settings(use_azure_di=True, azure_di_endpoint=None))
        assert result.endpoint_present is False

    def test_failure_reason_mentions_endpoint(self):
        result = _run_preflight(_make_settings(use_azure_di=True, azure_di_endpoint=None))
        assert result.failure_reason is not None
        assert "endpoint" in result.failure_reason.lower() or "ENDPOINT" in result.failure_reason


# ══════════════════════════════════════════════════════════════════════════════
# 3.  Key missing
# ══════════════════════════════════════════════════════════════════════════════

class TestKeyMissing:
    def test_fallback_when_key_none(self):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key=None,
        ))
        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_key_present_false(self):
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key=None,
        ))
        assert result.key_present is False

    def test_failure_reason_mentions_key(self):
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="",
        ))
        assert result.failure_reason is not None
        assert "key" in result.failure_reason.lower() or "KEY" in result.failure_reason


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Bad endpoint format
# ══════════════════════════════════════════════════════════════════════════════

class TestBadEndpointFormat:
    def _bad(self, ep):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=True, azure_di_endpoint=ep, azure_di_key="k",
        ))
        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE, \
            f"Expected fallback for endpoint={ep!r}"
        assert result.endpoint_valid is False

    def test_http_not_https(self):
        self._bad("http://myresource.cognitiveservices.azure.com")

    def test_missing_scheme(self):
        self._bad("myresource.cognitiveservices.azure.com")

    def test_single_word(self):
        self._bad("notanurl")

    def test_valid_format_accepted(self):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        ))
        # With ping skipped this should select azure_di
        assert result.selected_backend == ExtractionBackend.AZURE_DI
        assert result.endpoint_valid is True


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Happy path — all config present, ping skipped
# ══════════════════════════════════════════════════════════════════════════════

class TestHappyPath:
    def test_azure_di_selected_when_configured(self):
        from app.services.preflight import ExtractionBackend
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        ), skip_connectivity_ping=True)
        assert result.selected_backend == ExtractionBackend.AZURE_DI

    def test_all_flags_true(self):
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        ))
        assert result.azure_di_enabled is True
        assert result.endpoint_present is True
        assert result.key_present is True
        assert result.endpoint_valid is True

    def test_no_failure_reason(self):
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        ))
        assert result.failure_reason is None

    def test_ping_skipped_gives_none(self):
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        ), skip_connectivity_ping=True)
        assert result.connectivity_ok is None


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Connectivity ping fails → fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestConnectivityFails:
    def test_fallback_on_ping_failure(self):
        from app.services.preflight import ExtractionBackend
        import app.services.preflight as pf_module

        settings_obj = _make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        )
        with mock.patch.object(pf_module, "_connectivity_ping",
                               return_value=(False, "Azure DI auth failed (HTTP 403)")):
            with mock.patch.object(pf_module, "settings", settings_obj):
                result = pf_module.run_preflight_checks(skip_connectivity_ping=False)

        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE
        assert result.connectivity_ok is False
        assert "403" in (result.failure_reason or "") or "auth" in (result.failure_reason or "").lower()

    def test_fallback_on_network_error(self):
        from app.services.preflight import ExtractionBackend
        import app.services.preflight as pf_module

        settings_obj = _make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        )
        with mock.patch.object(pf_module, "_connectivity_ping",
                               return_value=(False, "connection refused")):
            with mock.patch.object(pf_module, "settings", settings_obj):
                result = pf_module.run_preflight_checks(skip_connectivity_ping=False)

        assert result.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_azure_di_selected_on_ping_success(self):
        from app.services.preflight import ExtractionBackend
        import app.services.preflight as pf_module

        settings_obj = _make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        )
        with mock.patch.object(pf_module, "_connectivity_ping",
                               return_value=(True, "")):
            with mock.patch.object(pf_module, "settings", settings_obj):
                result = pf_module.run_preflight_checks(skip_connectivity_ping=False)

        assert result.selected_backend == ExtractionBackend.AZURE_DI
        assert result.connectivity_ok is True


# ══════════════════════════════════════════════════════════════════════════════
# 7.  Notes never contain the secret key
# ══════════════════════════════════════════════════════════════════════════════

class TestSecretSafety:
    def test_notes_do_not_contain_key(self):
        secret_key = "super-secret-azure-key-abc123"
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key=secret_key,
        ))
        assert secret_key not in (result.notes or ""), \
            "Secret key must never appear in preflight notes"

    def test_failure_reason_does_not_contain_key(self):
        secret_key = "super-secret-azure-key-abc123"
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="http://bad-scheme-url",  # will fail format check
            azure_di_key=secret_key,
        ))
        assert secret_key not in (result.failure_reason or ""), \
            "Secret key must never appear in failure_reason"


# ══════════════════════════════════════════════════════════════════════════════
# 8.  _reset_azure_di_error
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerReset:
    def test_reset_clears_error(self):
        import app.services.extractor as ext
        ext._azure_di_error = "some previous error"
        assert ext._azure_di_error is not None

        ext._reset_azure_di_error()
        assert ext._azure_di_error is None

    def test_reset_idempotent_when_already_none(self):
        import app.services.extractor as ext
        ext._azure_di_error = None
        ext._reset_azure_di_error()  # must not raise
        assert ext._azure_di_error is None

    def test_azure_di_available_false_after_error_set(self):
        import app.services.extractor as ext
        import app.services.preflight as pf_module
        original = ext._azure_di_error
        try:
            ext._azure_di_error = "forced error"
            available, reason = ext.azure_di_available()
            assert available is False
            assert reason is not None
        finally:
            ext._azure_di_error = original

    def test_azure_di_available_false_after_reset_with_disabled_config(self):
        """After reset, azure_di_available() still returns False when USE_AZURE_DI=False."""
        import app.services.extractor as ext
        ext._reset_azure_di_error()
        # With USE_AZURE_DI disabled in settings, should still be False
        with mock.patch("app.services.extractor.settings") as ms:
            ms.use_azure_di = False
            ms.azure_di_endpoint = None
            ms.azure_di_key = None
            available, _ = ext.azure_di_available()
        assert available is False


# ══════════════════════════════════════════════════════════════════════════════
# 9.  Batch job has preflight integration
# ══════════════════════════════════════════════════════════════════════════════

class TestBatchIntegration:
    def _job_src(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _process_batch_job"):
                   src.find("\n\n@router.post(\"\", response_model=BatchOut)")]

    def test_preflight_imported_in_job(self):
        fn = self._job_src()
        assert "run_preflight_checks" in fn, \
            "_process_batch_job must call run_preflight_checks"

    def test_preflight_result_written_to_notes(self):
        fn = self._job_src()
        assert "preflight.notes" in fn, \
            "preflight.notes must be written to batch notes"

    def test_fallback_resets_circuit_breaker(self):
        fn = self._job_src()
        assert "_reset_azure_di_error" in fn, \
            "_process_batch_job must call _reset_azure_di_error on fallback path"

    def test_preflight_before_file_loop(self):
        fn = self._job_src()
        preflight_idx = fn.find("run_preflight_checks()")
        loop_idx      = fn.find("for file_index, invoice_file in enumerate(files")
        assert preflight_idx > 0, "preflight must be called in _process_batch_job"
        assert preflight_idx < loop_idx, \
            "preflight must be called BEFORE the file processing loop"

    def test_preflight_after_queued_note(self):
        fn = self._job_src()
        queued_idx    = fn.find("Queued {len(files)}")
        preflight_idx = fn.find("run_preflight_checks()")
        assert queued_idx < preflight_idx, \
            "preflight must run after the 'Queued' note is written"


# ══════════════════════════════════════════════════════════════════════════════
# 10.  Preflight module structure
# ══════════════════════════════════════════════════════════════════════════════

class TestPreflightModuleStructure:
    def test_preflight_module_parses(self):
        ast.parse(_src("app/services/preflight.py"))

    def test_extraction_backend_enum_values(self):
        from app.services.preflight import ExtractionBackend
        assert ExtractionBackend.AZURE_DI == "azure_di"
        assert ExtractionBackend.FALLBACK_NON_AZURE == "fallback_non_azure"

    def test_preflight_result_has_required_fields(self):
        from app.services.preflight import PreflightResult, ExtractionBackend
        r = PreflightResult(
            selected_backend=ExtractionBackend.FALLBACK_NON_AZURE,
            azure_di_enabled=False,
            endpoint_present=False,
            key_present=False,
            endpoint_valid=False,
            connectivity_ok=None,
            failure_reason="test",
            notes="test note",
            duration_ms=5,
        )
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE
        assert r.notes == "test note"
        assert r.duration_ms == 5

    def test_notes_field_populated_on_fallback(self):
        result = _run_preflight(_make_settings(use_azure_di=False))
        assert result.notes, "notes must be non-empty"
        assert "fallback" in result.notes.lower() or "unavailable" in result.notes.lower()

    def test_notes_field_populated_on_success(self):
        result = _run_preflight(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://myresource.cognitiveservices.azure.com",
            azure_di_key="validkey",
        ))
        assert result.notes, "notes must be non-empty"
        assert "azure" in result.notes.lower()

    def test_duration_ms_is_non_negative(self):
        result = _run_preflight(_make_settings(use_azure_di=False))
        assert result.duration_ms >= 0


# ══════════════════════════════════════════════════════════════════════════════
# 11.  Stable logic untouched
# ══════════════════════════════════════════════════════════════════════════════

class TestStableLogicUntouched:
    def test_bcrs_logic_unchanged(self):
        src = _src("app/routers/batches.py")
        bcrs = src[src.find("def _extract_bcrs_amount_from_summary"):
                   src.find("\ndef _build_bcrs_row")]
        assert "has_label_line" in bcrs
        assert "best_score < 20" in bcrs
        assert "_is_vat_line" in bcrs

    def test_split_math_unchanged(self):
        src = _src("app/routers/batches.py")
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src

    def test_remap_logic_unchanged(self):
        rv = _src("app/routers/review.py")
        assert "def save_remap" in rv
        assert "_read_region_text" in rv
        assert "selected_text" in rv

    def test_progress_tracking_unchanged(self):
        src = _src("app/routers/batches.py")
        fn = src[src.find("def _process_batch_job"):
                 src.find("\n\n@router.post(\"\", response_model=BatchOut)")]
        assert "InvoiceBatch.page_count < processed_pages" in fn
        assert "expire_on_commit=False" in src

    def test_all_files_parse(self):
        for f in ["app/routers/batches.py", "app/routers/review.py",
                  "app/services/extractor.py", "app/services/preflight.py"]:
            ast.parse(_src(f))
