"""
Regression tests for the Azure DI real readiness check and per-page timeout.

Key fixes tested:
  1. Readiness check uses GET not HEAD
  2. "available" / "ready" note only when GET returned HTTP 200
  3. Auth failure → NOT_READY → fallback
  4. Timeout → NOT_READY → fallback
  5. DNS/network error → NOT_READY → fallback
  6. Disabled → DISABLED → fallback
  7. skip_readiness_check=True → CONFIGURED (honest, not READY)
  8. Per-page timeout wraps poller.result() with ThreadPoolExecutor
  9. AzureDiReadinessState has all required states
 10. Batch job logs readiness_state not just selected_backend
 11. Notes never say "available" unless truly ready
 12. Stable logic untouched

Run: pytest tests/test_preflight.py -v
"""
from __future__ import annotations
import ast, os, types, unittest.mock as mock

def _src(f):
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()

def _make_settings(**kw):
    d = dict(use_azure_di=False, azure_di_endpoint=None, azure_di_key=None,
             azure_di_preflight_timeout=5.0, azure_di_page_timeout_s=45)
    d.update(kw); return types.SimpleNamespace(**d)

def _run(s, skip=True):
    import app.services.preflight as pf
    with mock.patch.object(pf, "settings", s):
        return pf.run_preflight_checks(skip_readiness_check=skip)


class TestReadinessStateEnum:
    def test_states_exist(self):
        from app.services.preflight import AzureDiReadinessState
        assert AzureDiReadinessState.DISABLED   == "disabled"
        assert AzureDiReadinessState.CONFIGURED == "configured"
        assert AzureDiReadinessState.READY      == "ready"
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

    def test_no_check_attempted_when_disabled(self):
        r = _run(_make_settings(use_azure_di=False), skip=False)
        assert r.readiness_ok is None


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


class TestConfiguredState:
    def test_configured_not_ready_when_skipped(self):
        from app.services.preflight import AzureDiReadinessState, ExtractionBackend
        r = _run(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key="k",
        ), skip=True)
        assert r.readiness_state  == AzureDiReadinessState.CONFIGURED
        assert r.selected_backend == ExtractionBackend.AZURE_DI

    def test_configured_note_does_not_say_available(self):
        r = _run(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key="k",
        ), skip=True)
        assert "available" not in r.notes.lower()
        assert "configured" in r.notes.lower() or "unverified" in r.notes.lower()

    def test_readiness_ok_is_none_when_skipped(self):
        r = _run(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key="k",
        ), skip=True)
        assert r.readiness_ok is None


class TestReadyState:
    def _mock_ready(self):
        import app.services.preflight as pf
        s = _make_settings(use_azure_di=True,
                           azure_di_endpoint="https://x.cognitiveservices.azure.com",
                           azure_di_key="k")
        with mock.patch.object(pf, "_readiness_check", return_value=(True, "")):
            with mock.patch.object(pf, "settings", s):
                return pf.run_preflight_checks(skip_readiness_check=False)

    def test_ready_state_on_200(self):
        from app.services.preflight import AzureDiReadinessState, ExtractionBackend
        r = self._mock_ready()
        assert r.readiness_state  == AzureDiReadinessState.READY
        assert r.selected_backend == ExtractionBackend.AZURE_DI
        assert r.readiness_ok     is True

    def test_ready_note_says_ready(self):
        r = self._mock_ready()
        assert "ready" in r.notes.lower()


class TestReadinessFailures:
    def _fail(self, reason):
        import app.services.preflight as pf
        s = _make_settings(use_azure_di=True,
                           azure_di_endpoint="https://x.cognitiveservices.azure.com",
                           azure_di_key="k")
        with mock.patch.object(pf, "_readiness_check", return_value=(False, reason)):
            with mock.patch.object(pf, "settings", s):
                return pf.run_preflight_checks(skip_readiness_check=False)

    def test_401_fallback(self):
        from app.services.preflight import ExtractionBackend, AzureDiReadinessState
        r = self._fail("auth failed (HTTP 401)")
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE
        assert r.readiness_state  == AzureDiReadinessState.NOT_READY
        assert r.readiness_ok     is False

    def test_403_fallback(self):
        from app.services.preflight import ExtractionBackend
        r = self._fail("auth failed (HTTP 403)")
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_timeout_fallback(self):
        from app.services.preflight import ExtractionBackend
        r = self._fail("timed out after 5s")
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_dns_fallback(self):
        from app.services.preflight import ExtractionBackend
        r = self._fail("network error: Name resolution failed")
        assert r.selected_backend == ExtractionBackend.FALLBACK_NON_AZURE

    def test_not_ready_note_no_available(self):
        r = self._fail("timeout")
        assert "available" not in r.notes.lower()
        assert "not ready" in r.notes.lower() or "fallback" in r.notes.lower()


class TestGetNotHead:
    def test_uses_get_not_head(self):
        pf_src = _src("app/services/preflight.py")
        fn = pf_src[pf_src.find("def _readiness_check"):
                    pf_src.find("\ndef run_preflight_checks")]
        assert 'method="GET"' in fn,   "_readiness_check must use GET"
        assert 'method="HEAD"' not in fn, "_readiness_check must NOT use HEAD"

    def test_401_returns_false_in_code(self):
        pf_src = _src("app/services/preflight.py")
        fn = pf_src[pf_src.find("def _readiness_check"):
                    pf_src.find("\ndef run_preflight_checks")]
        idx = fn.find("401")
        assert idx > 0
        assert "False" in fn[idx:idx+120]

    def test_403_returns_false_in_code(self):
        pf_src = _src("app/services/preflight.py")
        fn = pf_src[pf_src.find("def _readiness_check"):
                    pf_src.find("\ndef run_preflight_checks")]
        idx = fn.find("403")
        assert idx > 0
        assert "False" in fn[idx:idx+120]

    def test_reads_minimal_body(self):
        pf_src = _src("app/services/preflight.py")
        fn = pf_src[pf_src.find("def _readiness_check"):
                    pf_src.find("\ndef run_preflight_checks")]
        assert "resp.read(" in fn, "Must read response body to close socket cleanly"


class TestSecretSafety:
    def test_key_not_in_notes(self):
        import app.services.preflight as pf
        secret = "super-secret-key-xyz789"
        s = _make_settings(use_azure_di=True,
                           azure_di_endpoint="https://x.cognitiveservices.azure.com",
                           azure_di_key=secret)
        with mock.patch.object(pf, "_readiness_check", return_value=(False, "timeout")):
            with mock.patch.object(pf, "settings", s):
                r = pf.run_preflight_checks(skip_readiness_check=False)
        assert secret not in r.notes
        assert secret not in (r.failure_reason or "")

    def test_key_not_formatted_in_log_strings(self):
        pf_src = _src("app/services/preflight.py")
        fn = pf_src[pf_src.find("def run_preflight_checks"):]
        before_check = fn[:fn.find("_readiness_check(")]
        import re
        matches = re.findall(r'\{key\b|%[sr].*\bkey\b', before_check)
        assert not matches, "key value must not appear in log format strings"


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


class TestNoteTextContract:
    def test_disabled_says_disabled(self):
        r = _run(_make_settings(use_azure_di=False))
        assert "disabled" in r.notes.lower()
        assert "available" not in r.notes.lower()

    def test_not_ready_says_not_ready_or_fallback(self):
        r = _run(_make_settings(use_azure_di=True, azure_di_key="k"))
        assert "fallback" in r.notes.lower() or "not ready" in r.notes.lower()
        assert "available" not in r.notes.lower()

    def test_configured_says_configured_not_available(self):
        r = _run(_make_settings(
            use_azure_di=True,
            azure_di_endpoint="https://x.cognitiveservices.azure.com",
            azure_di_key="k",
        ), skip=True)
        assert "available" not in r.notes.lower()
        assert "configured" in r.notes.lower() or "unverified" in r.notes.lower()

    def test_ready_says_ready(self):
        import app.services.preflight as pf
        s = _make_settings(use_azure_di=True,
                           azure_di_endpoint="https://x.cognitiveservices.azure.com",
                           azure_di_key="k")
        with mock.patch.object(pf, "_readiness_check", return_value=(True, "")):
            with mock.patch.object(pf, "settings", s):
                r = pf.run_preflight_checks(skip_readiness_check=False)
        assert "ready" in r.notes.lower()


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
