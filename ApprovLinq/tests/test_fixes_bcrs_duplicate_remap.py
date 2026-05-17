"""
Targeted tests for the 4 fixed areas in this build.

1. BCRS/deposit split — no split from arithmetic mismatch alone; split on label evidence
2. Advisory mismatch review flag — totals_mismatch_advisory triggers review_required
3. Duplicate row endpoint — backend endpoint wired and returns correct payload
4. Structured totals parsing — Tax Analysis / Invoice Summary / Deposit Summary blocks

Run: pytest tests/test_fixes_bcrs_duplicate_remap.py -v
"""
from __future__ import annotations
import re, os, logging

logger = logging.getLogger(__name__)


# ── shared BCRS loader ─────────────────────────────────────────────────────────
def _load_bcrs():
    def _pm(t):
        vals=[]
        for m in re.findall(r"(?<!\d)(?:€\s*)?(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", t or ""):
            raw=m.replace(".","").replace(",",".") if re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$",m) else m.replace(",","")
            try: vals.append(round(float(raw),2))
            except: pass
        return vals
    def _fm(v): return (_pm(str(v or ""))+[None])[0]
    def _isc(l): return bool(re.search(r"\b(total|subtotal|gross|net|vat|tax|summary|amount due|total due|invoice summary|tax summary|deposit summary|total eur|total incl|total net|total gross)\b",(l or "").lower()))
    def _ibi(l): return bool(re.search(r"\b(qty|quantity|unit|uom|barcode|item|description|pcs|price|w/sale|retail|consumer|code|stock|salesperson|order no|delivery note)\b",(l or "").lower()))
    def _csr(payload):
        lines=[]
        tr=str(payload.get("totals_raw") or "")
        if tr: lines.extend([ln.strip() for ln in tr.splitlines() if ln.strip()])
        pt=str(payload.get("page_text_raw") or "")
        if pt:
            pl=[ln.strip() for ln in pt.splitlines() if ln.strip()]
            if pl:
                si=max(0,int(len(pl)*0.5)); lines.extend(pl[si:]); lines.extend(pl[-20:])
        deduped,seen=[],set()
        for ln in lines:
            k=re.sub(r"\s+"," ",ln)
            if k not in seen: seen.add(k); deduped.append(ln)
        return deduped
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers", "batches.py")).read()
    ns = {"re": re, "_parse_money_candidates": _pm, "_parse_first_money": _fm,
          "_is_summary_context": _isc, "_is_body_or_item_context": _ibi,
          "_collect_summary_region_lines": _csr}
    exec(src[src.find("\ndef _parse_money_candidates"):src.find("\ndef _build_bcrs_row")], ns)
    return ns["_extract_bcrs_amount_from_summary"]

_extract = _load_bcrs()


# ─────────────────────────────────────────────────────────────────────────────
# 1. BCRS detection
# ─────────────────────────────────────────────────────────────────────────────

class TestBcrsNoSplitOnArithmetic:
    """Arithmetic mismatch alone must never produce a split."""

    def test_deposit_component_detected_reason_alone(self):
        """deposit_component_detected in review_reasons with no label in text → None."""
        assert _extract({
            "review_reasons": "deposit_component_detected:2.40",
            "totals_raw": "Sub Total 200.00\nVAT 36.00\nInvoice Total 238.40",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 238.40,
        }) is None

    def test_clean_balanced_invoice(self):
        assert _extract({
            "totals_raw": "Sub Total 200.00\nVAT 36.00\nInvoice Total 236.00",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 236.0,
        }) is None

    def test_reconciliation_match_no_label(self):
        """diff=2.40 reconciles perfectly but there is no BCRS label → None."""
        assert _extract({
            "totals_raw": "Sub Total 100.00\nVAT 18.00\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        }) is None

    def test_zero_deposit_label(self):
        assert _extract({
            "totals_raw": "Deposit Summary\nDeposit 0.00\nNet 250.00\nVAT 45.00\nTotal 295.00",
            "net_amount": 250.0, "vat_amount": 45.0, "total_amount": 295.0,
        }) is None

    def test_item_row_deposit_ignored(self):
        assert _extract({
            "page_text_raw": (
                "Item  Description  Qty  Unit  Total\n"
                "001  Glass bottle deposit  24  0.10  2.40\n"
                "Sub Total 21.60\nVAT 3.89\nInvoice Total 25.49"
            ),
            "net_amount": 21.60, "vat_amount": 3.89, "total_amount": 25.49,
        }) is None


class TestBcrsSplitOnLabelEvidence:
    """Strong label evidence in totals/summary region must produce a split."""

    def test_explicit_bcrs(self):
        assert _extract({
            "totals_raw": "Net 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        }) == 2.40

    def test_bcrs_deposit_large(self):
        assert _extract({
            "totals_raw": "Net 400.00\nVAT 72.00\nBCRS Deposit 70.80\nTotal 542.80",
            "net_amount": 400.0, "vat_amount": 72.0, "total_amount": 542.80,
        }) == 70.80

    def test_bcrs_refundable_deposit(self):
        assert _extract({
            "totals_raw": "Sub Total 98.72\nVAT 17.77\nBCRS Refundable Deposit (M) 10.80\nTotal Due 127.29",
            "net_amount": 98.72, "vat_amount": 17.77, "total_amount": 127.29,
        }) == 10.80

    def test_deposit_summary_block(self):
        assert _extract({
            "totals_raw": "Deposit Summary\nDeposit 4.80\nNet 250.00\nVAT 45.00\nTotal 299.80",
            "net_amount": 250.0, "vat_amount": 45.0, "total_amount": 299.80,
        }) == 4.80

    def test_plain_deposit_in_totals(self):
        assert _extract({
            "totals_raw": "Sub Total 180.00\nVAT 32.40\nDeposit 2.40\nInvoice Total 214.80",
            "net_amount": 180.0, "vat_amount": 32.40, "total_amount": 214.80,
        }) == 2.40

    def test_schembri_style(self):
        assert _extract({
            "totals_raw": "Net Amount 80.00\nVAT Amount 14.40\nBCRS 7.20\nInvoice Total 101.60",
            "net_amount": 80.0, "vat_amount": 14.40, "total_amount": 101.60,
        }) == 7.20


# ─────────────────────────────────────────────────────────────────────────────
# 2. Advisory mismatch review flag
# ─────────────────────────────────────────────────────────────────────────────

class TestAdvisoryMismatchReviewFlag:
    """When arithmetic mismatch looks deposit-shaped but no label evidence
    exists, the row must be flagged for review rather than auto-split."""

    def test_validate_invoice_emits_advisory_reason(self):
        src = open("app/services/validate_invoice.py").read()
        assert "totals_mismatch_advisory" in src, \
            "validate_invoice must emit totals_mismatch_advisory alongside deposit_component_detected"

    def test_advisory_reason_emitted_with_deposit_component(self):
        """Both deposit_component_detected AND totals_mismatch_advisory must be
        emitted together so the review engine can flag the row."""
        src = open("app/services/validate_invoice.py").read()
        block = src[src.find("if _is_deposit_amount"):src.find("else:\n                result.other_charges_amount")]
        assert "deposit_component_detected" in block
        assert "totals_mismatch_advisory" in block

    def test_review_engine_scores_advisory_as_medium(self):
        src = open("app/services/review_engine.py").read()
        idx = src.find("totals_mismatch_advisory")
        assert idx > 0, "totals_mismatch_advisory must be in review_engine reason weights"
        context = src[idx:idx+100]
        assert "medium" in context, "totals_mismatch_advisory must have medium priority"

    def test_deposit_component_not_auto_approvable(self):
        """deposit_component_detected must no longer be auto-approvable so rows
        with arithmetic mismatches always get flagged for human review."""
        src = open("app/services/review_engine.py").read()
        auto_block = src[src.find("_AUTO_APPROVABLE_REASONS"):src.find("_AUTO_APPROVABLE_REASONS") + 300]
        assert "deposit_component_detected" not in auto_block, \
            "deposit_component_detected must be removed from _AUTO_APPROVABLE_REASONS"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Duplicate row endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateRowEndpoint:
    """Checks the backend duplicate-row endpoint structure."""

    def test_endpoint_defined(self):
        src = open("app/routers/review.py").read()
        assert "/duplicate" in src

    def test_endpoint_post_method(self):
        src = open("app/routers/review.py").read()
        # The route decorator must be POST
        dup_idx = src.find("/duplicate")
        router_before = src[:dup_idx].rfind("@router.")
        assert "post" in src[router_before:dup_idx].lower()

    def test_duplicate_sets_review_required(self):
        src = open("app/routers/review.py").read()
        fn_start = src.find("def duplicate_row")
        fn_end   = src.find("\n@router", fn_start + 1)
        fn = src[fn_start:fn_end]
        assert "review_required=True" in fn

    def test_duplicate_sets_reason(self):
        src = open("app/routers/review.py").read()
        fn_start = src.find("def duplicate_row")
        fn_end   = src.find("\n@router", fn_start + 1)
        fn = src[fn_start:fn_end]
        assert "manually_duplicated_for_bcrs" in fn

    def test_duplicate_returns_duplicate_id(self):
        src = open("app/routers/review.py").read()
        fn_start = src.find("def duplicate_row")
        fn_end   = src.find("\n@router", fn_start + 1)
        fn = src[fn_start:fn_end]
        assert "duplicate_id" in fn

    def test_duplicate_writes_audit_on_original(self):
        src = open("app/routers/review.py").read()
        fn_start = src.find("def duplicate_row")
        fn_end   = src.find("\n@router", fn_start + 1)
        fn = src[fn_start:fn_end]
        assert "InvoiceRowFieldAudit" in fn

    def test_duplicate_sets_method_used(self):
        """method_used='manual_duplicate' distinguishes it from extracted rows."""
        src = open("app/routers/review.py").read()
        fn_start = src.find("def duplicate_row")
        fn_end   = src.find("\n@router", fn_start + 1)
        fn = src[fn_start:fn_end]
        assert "manual_duplicate" in fn


# ─────────────────────────────────────────────────────────────────────────────
# 4. Duplicate row UI
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateRowUI:
    def test_button_in_html(self):
        html = open("app/static/review.html").read()
        assert "duplicateRowBtn" in html

    def test_handler_in_js(self):
        js = open("app/static/js/review.js").read()
        assert "duplicateRowBtn" in js

    def test_js_calls_duplicate_endpoint(self):
        js = open("app/static/js/review.js").read()
        assert "/duplicate" in js

    def test_js_auto_selects_duplicate(self):
        """After creation the JS should auto-select the new row."""
        js = open("app/static/js/review.js").read()
        assert "data.duplicate_id" in js


# ─────────────────────────────────────────────────────────────────────────────
# 5. Structured totals parsing (Tax Analysis / Invoice Summary / Deposit Summary)
# ─────────────────────────────────────────────────────────────────────────────

def _load_struct():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "services", "extractor.py")).read()
    start = src.find("def _extract_structured_summary_totals")
    end   = src.find("\ndef simple_extract")
    ns = {"re": re, "logger": logger}
    exec(src[start:end], ns)
    return ns["_extract_structured_summary_totals"]

_struct = _load_struct()

FARSONS_TEXT = """
Tax Analysis
Gross Value          180.00
Total Discount         0.00
Tax Amount            32.40
TOTAL               212.40

Invoice Summary
Sub Total           180.00
Less Returns          0.00
Invoice Total       212.40

Deposit Summary
Deposits              9.60
"""


class TestStructuredTotals:

    def test_farsons_vat_from_tax_analysis(self):
        r = _struct(FARSONS_TEXT)
        assert r is not None
        assert r.get("vat_amount") == 32.40

    def test_farsons_total_from_invoice_summary(self):
        r = _struct(FARSONS_TEXT)
        assert r is not None
        assert r.get("total_amount") == 212.40

    def test_farsons_deposit_from_deposit_summary(self):
        r = _struct(FARSONS_TEXT)
        assert r is not None
        assert r.get("_deposit_candidate") == 9.60

    def test_farsons_gross_value_not_used_as_net(self):
        r = _struct(FARSONS_TEXT)
        assert r is not None
        assert r.get("net_amount") != 212.40, "Gross Value must not become the net amount"

    def test_simple_invoice_no_structured_block(self):
        assert _struct("Sub Total 200\nVAT 36\nTotal 236") is None

    def test_mismatched_totals_returns_none(self):
        text = "\nTax Analysis\nTax Amount 10\nTOTAL 100\n\nInvoice Summary\nInvoice Total 999\n"
        assert _struct(text) is None

    def test_no_supplier_names_hardcoded(self):
        src = open("app/services/extractor.py").read()
        fn_start = src.find("def _extract_structured_summary_totals")
        fn_end   = src.find("\ndef simple_extract")
        fn = src[fn_start:fn_end]
        for name in ("farsons", "simonds", "busuttil", "sultana", "maverick", "nectar", "amsm"):
            assert name.lower() not in fn.lower(), f"Supplier name '{name}' must not be hardcoded"

    def test_helper_called_in_simple_extract(self):
        src = open("app/services/extractor.py").read()
        assert "_struct = _extract_structured_summary_totals" in src


# ─────────────────────────────────────────────────────────────────────────────
# 6. Remap JS reliability guards
# ─────────────────────────────────────────────────────────────────────────────

class TestRemapJsGuards:
    def test_image_loaded_guard(self):
        js = open("app/static/js/review.js").read()
        assert "naturalWidth" in js, "drag start must check image.naturalWidth"

    def test_image_complete_guard(self):
        js = open("app/static/js/review.js").read()
        assert "previewImg.complete" in js

    def test_future_hint_message(self):
        js = open("app/static/js/review.js").read()
        assert "future remap hint" in js.lower()

    def test_saved_as_hint_read(self):
        js = open("app/static/js/review.js").read()
        assert "saved_as_hint" in js

    def test_remap_upsert_in_backend(self):
        src = open("app/routers/review.py").read()
        assert "existing_hint" in src
        assert "saved_as_hint" in src

    def test_apply_remap_hints_checks_review_fields(self):
        src = open("app/routers/batches.py").read()
        rh = src[src.find("def _apply_remap_hints"):src.find("def _parse_money_candidates")]
        assert "_review_fields" in rh

    def test_apply_remap_hints_prefers_supplier_id(self):
        src = open("app/routers/batches.py").read()
        rh = src[src.find("def _apply_remap_hints"):src.find("def _parse_money_candidates")]
        assert "supplier_id" in rh
