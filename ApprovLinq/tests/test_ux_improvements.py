"""
Tests for UX/workflow improvements and BCRS false-positive prevention.

Items covered:
  1-2.  Collapsible scanner sections
  3.    Low-detection toast removed
  4-5.  Review PDF preview panel removed
  6.    Remap lock (remapLockReason)
  7.    RemapHint upsert (no duplicates)
  8.    review_reasons + method_used in API payload
  9.    Tool badge derivation (DI / AI / OCR / TXT)
  10.   _apply_remap_hints in batches pipeline
  11.   BCRS false-positive prevention
"""
from __future__ import annotations
import re, os


# ── BCRS helpers ──────────────────────────────────────────────────────────────
def _pm(text):
    vals=[]
    for m in re.findall(r"(?<!\d)(?:€\s*)?(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", text or ""):
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

def _load_extract():
    src=open(os.path.join(os.path.dirname(__file__),"..","app","routers","batches.py")).read()
    start=src.find("\ndef _parse_money_candidates"); end=src.find("\ndef _build_bcrs_row")
    ns={"re":re,"_parse_money_candidates":_pm,"_parse_first_money":_fm,
        "_is_summary_context":_isc,"_is_body_or_item_context":_ibi,
        "_collect_summary_region_lines":_csr}
    exec(src[start:end],ns)
    return ns["_extract_bcrs_amount_from_summary"]
_extract=_load_extract()


class TestCollapsibleSections:
    def test_batches_wrapper(self):
        h=open("app/static/scanner.html").read()
        assert 'id="batchesSectionBody"' in h and 'id="batchesSectionToggle"' in h
    def test_rows_wrapper(self):
        h=open("app/static/scanner.html").read()
        assert 'id="rowsSectionBody"' in h and 'id="rowsSectionToggle"' in h
    def test_wire_collapsible(self):
        assert "wireCollapsible" in open("app/static/js/app.js").read()
    def test_session_storage(self):
        assert "sessionStorage" in open("app/static/js/app.js").read()
    def test_css(self):
        assert "section-collapsed" in open("app/static/css/components.css").read()


class TestToastRemoved:
    def test_toast_absent(self):
        assert "Low-confidence fields detected" not in open("app/static/js/app.js").read()
    def test_review_cell_kept(self):
        js=open("app/static/js/app.js").read()
        assert "renderReviewCell" in js and "Review now" in js
    def test_row_flash_kept(self):
        assert "row-flash" in open("app/static/js/app.js").read()


class TestPreviewRemoved:
    def test_preview_hidden(self):
        h=open("app/static/review.html").read()
        assert 'id="previewWrap"' in h and "display:none" in h
    def test_stubs_present(self):
        h=open("app/static/review.html").read()
        for e in ("previewImg","remapSelection","remapHint"): assert f'id="{e}"' in h
    def test_single_col(self):
        h=open("app/static/review.html").read()
        assert "grid-template-columns:1fr" in h and "1fr 1fr" not in h


class TestRemapLock:
    def test_function_defined(self):
        assert "function remapLockReason" in open("app/static/js/review.js").read()
    def test_exported_check(self):
        assert '"exported"' in open("app/static/js/review.js").read()
    def test_row_reviewed_check(self):
        assert "row_reviewed" in open("app/static/js/review.js").read()
    def test_lock_in_mouseup(self):
        js=open("app/static/js/review.js").read()
        assert js.find("remapLockReason()",js.find("mouseup")) > js.find("mouseup")
    def test_lock_in_change(self):
        js=open("app/static/js/review.js").read()
        assert js.find("remapLockReason()",js.find('"change"')) > js.find('"change"')


class TestRemapHintUpsert:
    def test_upsert_present(self):
        py=open("app/routers/review.py").read()
        assert "existing_hint" in py and "existing_hint.x = payload.x" in py
    def test_else_branch(self):
        assert "else:\n        hint = RemapHint(" in open("app/routers/review.py").read()


class TestApiPayload:
    def test_review_reasons(self):
        assert '"review_reasons"' in open("app/routers/review.py").read()
    def test_method_used(self):
        assert '"method_used"' in open("app/routers/review.py").read()
    def test_pipe_split(self):
        assert '.split("|")' in open("app/routers/review.py").read()
    def test_js_reads_reasons(self):
        js=open("app/static/js/review.js").read()
        assert "r.review_reasons" in js and "REASON_LABELS" in js
    def test_js_reads_method(self):
        assert "r.method_used" in open("app/static/js/review.js").read()


class TestToolBadge:
    def _b(self,m):
        m=(m or "").lower()
        if "azure_di" in m or "_di" in m: return "DI"
        if "openai" in m or "vision" in m or "_ai" in m: return "AI"
        if "ocr" in m: return "OCR"
        if m: return "TXT"
        return "-"
    def test_di(self):    assert self._b("native_text+azure_di")=="DI"
    def test_ocr(self):   assert self._b("ocr_ocr_space")=="OCR"
    def test_txt(self):   assert self._b("native_text")=="TXT"
    def test_ai(self):    assert self._b("native_text+openai_text")=="AI"
    def test_empty(self): assert self._b("")=="-"
    def test_none(self):  assert self._b(None)=="-"
    def test_in_review_js(self):
        js=open("app/static/js/review.js").read()
        assert "toolBadge" in js and "azure_di" in js
    def test_in_app_js(self): assert "toolBadge" in open("app/static/js/app.js").read()
    def test_css(self):
        c=open("app/static/css/components.css").read()
        for cl in (".tool-di",".tool-ai",".tool-ocr",".tool-native"): assert cl in c


class TestApplyRemapHints:
    def test_defined(self): assert "def _apply_remap_hints" in open("app/routers/batches.py").read()
    def test_called(self):  assert "_apply_remap_hints(db, batch, row)" in open("app/routers/batches.py").read()
    def test_order(self):
        s=open("app/routers/batches.py").read()
        assert 0 < s.find("_apply_remap_hints(db, batch, row)") < s.find("_apply_saved_rules(db, batch, row)")
    def test_imported(self):
        s=open("app/routers/batches.py").read()
        assert "RemapHint" in s[:s.find("def ")]


class TestBcrsFalsePositives:
    def test_arithmetic_shortcut_removed(self):
        assert _extract({"review_reasons":"deposit_component_detected:2.40","totals_raw":"Sub Total  200.00\nVAT  36.00\nInvoice Total  238.40","net_amount":200.0,"vat_amount":36.0,"total_amount":238.40}) is None
    def test_clean_balanced(self):
        assert _extract({"totals_raw":"Sub Total  200.00\nVAT  36.00\nInvoice Total  236.00","net_amount":200.0,"vat_amount":36.0,"total_amount":236.0}) is None
    def test_zero_deposit(self):
        assert _extract({"totals_raw":"Deposit Summary\nDeposit  0.00\nNet  250.00\nVAT  45.00\nTotal  295.00","net_amount":250.0,"vat_amount":45.0,"total_amount":295.0}) is None
    def test_deposit_heading_balanced(self):
        assert _extract({"totals_raw":"Deposit Summary\nTotal Net  180.00\nVAT  32.40\nTotal  212.40","net_amount":180.0,"vat_amount":32.40,"total_amount":212.40}) is None
    def test_reconciliation_alone(self):
        assert _extract({"totals_raw":"Sub Total 100.00\nVAT 18.00\nTotal 120.40","net_amount":100.0,"vat_amount":18.0,"total_amount":120.40}) is None
    def test_explicit_bcrs(self):
        assert _extract({"totals_raw":"Net 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40","net_amount":100.0,"vat_amount":18.0,"total_amount":120.40})==2.40
    def test_bcrs_deposit_large(self):
        assert _extract({"totals_raw":"Net 400.00\nVAT 72.00\nBCRS Deposit 70.80\nTotal 542.80","net_amount":400.0,"vat_amount":72.0,"total_amount":542.80})==70.80
    def test_deposit_summary_real(self):
        assert _extract({"totals_raw":"Deposit Summary\nDeposit  4.80\nNet  250.00\nVAT  45.00\nTotal  299.80","net_amount":250.0,"vat_amount":45.0,"total_amount":299.80})==4.80
    def test_structural_shortcut_absent(self):
        s=open("app/routers/batches.py").read()
        fn=s.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "return round(float(m.group(1)), 2)" not in fn
    def test_threshold_20(self):
        s=open("app/routers/batches.py").read()
        fn=s.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "best_score < 20" in fn
    def test_label_line_guard(self):
        s=open("app/routers/batches.py").read()
        fn=s.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "has_label_line" in fn
