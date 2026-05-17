"""
Regression tests for the Azure DI preflight and per-page timeout.

This version reflects the updated preflight design:
  - No standalone network readiness ping.
  - The actual Azure DI extraction call is the source of truth.
  - Preflight only validates configuration (flag + endpoint format + key present).
  - READY state has been removed; CONFIGURED is the "proceed" state.
  - skip_readiness_check parameter is retained for API compat but is a no-op.

Key behaviours tested:
  1. Disabled → DISABLED → fallback
  2. Missing endpoint → NOT_READY → fallback
  3. Missing key → NOT_READY → fallback
  4. Bad endpoint format → NOT_READY → fallback
  5. Valid config (skip=True or skip=False) → CONFIGURED → azure_di
  6. readiness_ok always None (no network call)
  7. Notes never say "available"; say "configured" or "disabled" / "fallback"
  8. Per-page timeout still wraps poller.result() with ThreadPoolExecutor
  9. Batch integration: preflight runs before file loop, notes written
 10. Secrets never appear in notes or logged format strings
 11. Stable logic (BCRS, remap, fallback chain) untouched

Run: pytest tests/test_preflight.py -v
"""
from __future__ import annotations
import ast, os, types, unittest.mock as mock


def _src(f):
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()


def _make_settings(**kw):
    d = dict(use_azure_di=False, azure_di_endpoint=None, azure_di_key=None,
             azure_di_page_timeout_s=45)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _run(s, skip=True):
    import app.services.preflight as pf
    with mock.patch.object(pf, "settings", s):
        return pf.run_preflight_checks(skip_readiness_check=skip)


# ---------------------------------------------------------------------------
class TestReadinessStateEnum:
    def test_states_exist(self):
        from app.services.preflight import AzureDiReadinessState
        assert AzureDiReadinessState.DISABLED   == "disabled"
        assert AzureDiReadinessState.CONFIGURED == "configured"
        assert AzureDiReadinessState.NOT_READY  == "not_ready"

    def test_result_has_readiness_state_field(self):
        from app.services.preflight import PreflightResult, ExtractionBackend, AzureDiReadinessState
        r = PreflightResult(
            selected_backend=ExtractionBackend.FALLBACK_NON_AZURE,
            readiness_state=AzureDiReadinessState.DISABLED,
            azure_di_enabled=False, endpoint_present=False, key_present=False,
            endpoint_valid=False, readiness_ok=None, failure_reason=None, notes="x",
        )
        assert r.readiness_state == AzureDiReadinessState.DISABLED


# ---------------------------------------------------------------------------
class TestDisabledPath:
    def test_fallback_state_disabled(self):
        from app.services.preflight import ExtractionBackend, AzureDiReadinessState
        r = _run(_make_settings(use_azure_di=False))
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE
        assert r.readiness_state  == AzureDiReadinessState.DISABLED

    def test_notes_say_disabled_not_available(self):
        r = _run(_make_settings(use_azure_di=False))
        assert "available" not in r.notes.lower()
        assert "disabled" in r.notes.lower()

    def test_no_network_check_when_disabled(self):
        # readiness_ok is always None — no network call is made
        r = _run(_make_settings(use_azure_di=False), skip=False)
        assert r.readiness_ok is None


# ---------------------------------------------------------------------------
class TestConfigProblems:
    def _assert_not_ready(self, s):
        from app.services.preflight import AzureDiReadinessState, ExtractionBackend
        r = _run(s)
        assert r.readiness_state  == AzureDiReadinessState.NOT_READY
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE
        assert "available" not in r.notes.lower()

    def test_missing_endpoint(self):
        self._assert_not_ready(_make_settings(use_azure_di=True, azure_di_key="k"))

    def test_missing_key(self):
        self._assert_not_ready(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
        ))

    def test_bad_endpoint_http(self):
        self._assert_not_ready(_make_settings(
            use_azure_di=True, azure_di_endpoint="http://x.com", azure_di_key="k",
        ))

    def test_bad_endpoint_no_scheme(self):
        self._assert_not_ready(_make_settings(
            use_azure_di=True, azure_di_endpoint="notanurl", azure_di_key="k",
        ))


# ---------------------------------------------------------------------------
class TestConfiguredState:
    """With valid config, preflight selects AZURE_DI regardless of skip flag."""

    def _valid_settings(self):
        return _make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key="k",
        )

    def test_configured_state_skip_true(self):
        from app.services.preflight import AzureDiReadinessState, ExtractionBackend
        r = _run(self._valid_settings(), skip=True)
        assert r.readiness_state  == AzureDiReadinessState.CONFIGURED
        assert r.selected_backend == ExtractionBackend.AZURE_DI

    def test_configured_state_skip_false(self):
        """skip_readiness_check=False is a no-op; still CONFIGURED, no network call."""
        from app.services.preflight import AzureDiReadinessState, ExtractionBackend
        r = _run(self._valid_settings(), skip=False)
        assert r.readiness_state  == AzureDiReadinessState.CONFIGURED
        assert r.selected_backend == ExtractionBackend.AZURE_DI

    def test_readiness_ok_always_none(self):
        """No network call is made — readiness_ok is always None."""
        r = _run(self._valid_settings(), skip=True)
        assert r.readiness_ok is None
        r2 = _run(self._valid_settings(), skip=False)
        assert r2.readiness_ok is None

    def test_note_does_not_say_available(self):
        r = _run(self._valid_settings())
        assert "available" not in r.notes.lower()

    def test_note_says_configured_or_attempting(self):
        r = _run(self._valid_settings())
        assert "configured" in r.notes.lower() or "attempting" in r.notes.lower()


# ---------------------------------------------------------------------------
class TestNoNetworkPreflight:
    """Verify the network ping function has been fully removed."""

    def test_no_readiness_check_function(self):
        """_readiness_check must not exist in the new preflight module."""
        import app.services.preflight as pf
        assert not hasattr(pf, "_readiness_check"), (
            "_readiness_check still exists — the network ping was not removed"
        )

    def test_no_urllib_import(self):
        pf_src = _src("app/services/preflight.py")
        assert "urllib.request" not in pf_src, (
            "urllib.request found in preflight — network code was not removed"
        )

    def test_no_ready_state_in_enum(self):
        """READY enum value should no longer exist."""
        from app.services.preflight import AzureDiReadinessState
        assert not hasattr(AzureDiReadinessState, "READY"), (
            "READY state still exists — remove it; CONFIGURED is the proceed state"
        )

    def test_no_false_404_message(self):
        """The misleading 'endpoint not found (HTTP 404)' string must not appear."""
        pf_src = _src("app/services/preflight.py")
        assert "readiness check FAILED" not in pf_src
        assert "endpoint not found (HTTP 404)" not in pf_src


# ---------------------------------------------------------------------------
class TestSecretSafety:
    def test_key_not_in_notes(self):
        import app.services.preflight as pf
        secret = "super-secret-key-xyz789"
        s = _make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key=secret,
        )
        with mock.patch.object(pf, "settings", s):
            r = pf.run_preflight_checks()
        assert secret not in r.notes
        assert secret not in (r.failure_reason or "")

    def test_key_not_formatted_in_log_strings(self):
        pf_src = _src("app/services/preflight.py")
        fn = pf_src[pf_src.find("def run_preflight_checks"):]
        import re
        matches = re.findall(r'\{key\b|%[sr].*\bkey\b', fn)
        assert not matches, "key value must not appear in log format strings"


# ---------------------------------------------------------------------------
class TestPerPageTimeout:
    def test_poller_wrapped_with_timeout(self):
        ext_src = _src("app/services/extractor.py")
        fn_s = ext_src.find("def azure_di_extract_invoice(")
        fn_e = ext_src.find("\n\ndef openai_extract_invoice_fields", fn_s)
        fn = ext_src[fn_s:fn_e]
        assert "ThreadPoolExecutor" in fn, "Must use ThreadPoolExecutor for timeout"
        assert "TimeoutError" in fn,       "Must handle TimeoutError"

    def test_timeout_opens_circuit_breaker(self):
        ext_src = _src("app/services/extractor.py")
        fn_s = ext_src.find("def azure_di_extract_invoice(")
        fn_e = ext_src.find("\n\ndef openai_extract_invoice_fields", fn_s)
        fn = ext_src[fn_s:fn_e]
        timeout_idx = fn.find("TimeoutError")
        after = fn[timeout_idx:timeout_idx+300]
        assert "_azure_di_error" in after, "Timeout must open circuit breaker"
        assert "return None" in after,     "Timeout must return None"

    def test_timeout_configurable(self):
        assert "azure_di_page_timeout_s" in _src("app/services/extractor.py")


# ---------------------------------------------------------------------------
class TestExtractorErrorMessages:
    """Verify extractor.py classifies real DI failures accurately."""

    def _fn(self):
        src = _src("app/services/extractor.py")
        fn_s = src.find("def azure_di_extract_invoice(")
        fn_e = src.find("\n\ndef openai_extract_invoice_fields", fn_s)
        return src[fn_s:fn_e]

    def test_401_classified(self):
        fn = self._fn()
        assert "401" in fn
        assert "authentication" in fn.lower() or "Unauthorized" in fn

    def test_403_classified(self):
        fn = self._fn()
        assert "403" in fn
        assert "authoris" in fn.lower() or "Forbidden" in fn or "VNet" in fn

    def test_404_classified(self):
        fn = self._fn()
        assert "404" in fn

    def test_429_classified(self):
        fn = self._fn()
        assert "429" in fn

    def test_permanent_errors_open_circuit_breaker(self):
        fn = self._fn()
        # Permanent path must assign _azure_di_error
        perm_idx = fn.find("is_permanent = True")
        assert perm_idx > 0
        after = fn[perm_idx:]
        assert "_azure_di_error = " in after


# ---------------------------------------------------------------------------
class TestBatchIntegration:
    def _fn(self):
        src = _src("app/routers/batches.py")
        return src[src.find("def _process_batch_job"):
                   src.find("\n\n@router.post(\"\", response_model=BatchOut)")]

    def test_readiness_state_logged(self):
        assert "readiness_state" in self._fn()

    def test_preflight_before_file_loop(self):
        fn = self._fn()
        pf_i   = fn.find("run_preflight_checks()")
        loop_i = fn.find("for file_index, invoice_file in enumerate(files")
        assert pf_i > 0 and pf_i < loop_i

    def test_notes_written(self):
        assert "preflight.notes" in self._fn()

    def test_reset_on_fallback(self):
        assert "_reset_azure_di_error" in self._fn()


# ---------------------------------------------------------------------------
class TestNoteTextContract:
    def test_disabled_says_disabled(self):
        r = _run(_make_settings(use_azure_di=False))
        assert "disabled" in r.notes.lower()
        assert "available" not in r.notes.lower()

    def test_not_ready_says_fallback(self):
        r = _run(_make_settings(use_azure_di=True, azure_di_key="k"))
        assert "fallback" in r.notes.lower() or "not configured" in r.notes.lower()
        assert "available" not in r.notes.lower()

    def test_configured_says_configured_not_available(self):
        r = _run(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key="k",
        ), skip=True)
        assert "available" not in r.notes.lower()
        assert "configured" in r.notes.lower() or "attempting" in r.notes.lower()


# ---------------------------------------------------------------------------
class TestStableLogic:
    def test_bcrs_unchanged(self):
        src = _src("app/routers/batches.py")
        bcrs = src[src.find("def _extract_bcrs_amount_from_summary"):
                   src.find("\ndef _build_bcrs_row")]
        assert "has_label_line" in bcrs
        assert "best_score < 20" in bcrs

    def test_split_math_unchanged(self):
        assert "_corrected_total = round(_net + _vat, 2)" in _src("app/routers/batches.py")

    def test_remap_unchanged(self):
        rv = _src("app/routers/review.py")
        assert "def save_remap" in rv
        assert "selected_text" in rv

    def test_fallback_chain_preserved(self):
        src = _src("app/services/extractor.py")
        fn = src[src.find("def process_pdf_page("):src.find("def process_pdf_page_rows(")]
        assert "openai_extract_invoice_vision" in fn
        assert "simple_extract(" in fn

    def test_all_files_parse(self):
        for f in ["app/services/preflight.py", "app/services/extractor.py",
                  "app/routers/batches.py", "app/routers/review.py"]:
            ast.parse(_src(f))
