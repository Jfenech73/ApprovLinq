"""
Tests for BCRS/deposit detection — false-positive prevention and split correctness.

Root causes fixed:
  1. Arithmetic shortcut removed: deposit_component_detected in review_reasons
     no longer bypasses label+region checking.
  2. Score threshold raised 12 → 20: prevents weak reconciliation-only candidates.
  3. has_label_line guard: requires a non-pure-totals line with BCRS/deposit keyword.
  4. Scanned-page fix: azure_di_extract_invoice now returns di_page_text (result.content),
     which is propagated through merge_ai_fields into extracted and used in the
     _text_is_unavailable branch to build real totals_raw / page_text_raw so the
     BCRS detector can find labelled deposit amounts on scanned invoices.

Run: pytest tests/test_bcrs_false_positives.py -v
"""
from __future__ import annotations
import re, os

# ── inline helpers (mirror batches.py) ───────────────────────────────────────
def _pm(text):
    vals = []
    for m in re.findall(r"(?<!\d)(?:€\s*)?(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", text or ""):
        raw = m.replace(".","").replace(",",".") if re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$",m) else m.replace(",","")
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
    src = open(os.path.join(os.path.dirname(__file__),"..","app","routers","batches.py")).read()
    start = src.find("\ndef _parse_money_candidates")
    end   = src.find("\ndef _build_bcrs_row")
    ns = {"re":re,"_parse_money_candidates":_pm,"_parse_first_money":_fm,
          "_is_summary_context":_isc,"_is_body_or_item_context":_ibi,
          "_collect_summary_region_lines":_csr}
    exec(src[start:end], ns)
    return ns["_extract_bcrs_amount_from_summary"]

_extract = _load_extract()

def _scanned(di_content, net, vat, total):
    """Simulate the scanned-page payload after di_page_text fix."""
    lines = [ln.strip() for ln in di_content.splitlines() if ln.strip()]
    return {
        "totals_raw":    "\n".join(lines[-15:]) if lines else None,
        "page_text_raw": di_content[:20000],
        "net_amount": net, "vat_amount": vat, "total_amount": total,
    }

def _digital(page_text, net, vat, total, review_reasons=""):
    """Simulate a digital PDF page (native text available)."""
    parts = [f"Net: {net:.2f}", f"VAT: {vat:.2f}", f"Total: {total:.2f}"]
    return {
        "totals_raw":    " | ".join(parts),
        "page_text_raw": page_text[:20000],
        "review_reasons": review_reasons,
        "net_amount": net, "vat_amount": vat, "total_amount": total,
    }


# ── A. Positive cases — must split ───────────────────────────────────────────

class TestGenuineBcrs:
    """Genuine BCRS/deposit lines in the totals — must always split."""

    def test_explicit_bcrs_digital(self):
        assert _extract(_digital(
            "Sub Total 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40",
            100.0, 18.0, 120.40)) == 2.40

    def test_bcrs_deposit_large_digital(self):
        assert _extract(_digital(
            "Sub Total 400.00\nVAT 72.00\nBCRS Deposit 70.80\nTotal 542.80",
            400.0, 72.0, 542.80)) == 70.80

    def test_bcrs_refundable_deposit_digital(self):
        assert _extract(_digital(
            "Sub Total 98.72\nVAT 17.77\nBCRS Refundable Deposit (M) 10.80\nTotal Due 127.29",
            98.72, 17.77, 127.29)) == 10.80

    def test_schembri_bcrs_digital(self):
        assert _extract(_digital(
            "Net Amount 80.00\nVAT Amount 14.40\nBCRS 7.20\nInvoice Total 101.60",
            80.0, 14.40, 101.60)) == 7.20

    def test_nectar_bcrs_deposit_digital(self):
        assert _extract(_digital(
            "Net 200.00\nVAT 36.00\nBCRS Deposit 2.40\nTotal 238.40",
            200.0, 36.0, 238.40)) == 2.40

    def test_amsm_bcrs_digital(self):
        assert _extract(_digital(
            "Sub Total 200.00\nVAT 36.00\nBCRS 14.40\nTotal 250.40",
            200.0, 36.0, 250.40)) == 14.40

    def test_deposit_summary_real_digital(self):
        assert _extract(_digital(
            "Deposit Summary\nDeposit 4.80\nNet 250.00\nVAT 45.00\nTotal 299.80",
            250.0, 45.0, 299.80)) == 4.80

    def test_plain_deposit_in_totals_digital(self):
        assert _extract(_digital(
            "Sub Total 180.00\nVAT 32.40\nDeposit 2.40\nInvoice Total 214.80",
            180.0, 32.40, 214.80)) == 2.40

    # Scanned paths — di_page_text provides the labelled text
    def test_maverick_bcrs_scanned(self):
        assert _extract(_scanned(
            "Maverick Trading\nSub Total 98.72\nVAT 17.77\nBCRS Refundable Deposit (M) 10.80\nTotal Due 127.29",
            98.72, 17.77, 127.29)) == 10.80

    def test_amsm_bcrs_scanned(self):
        assert _extract(_scanned(
            "AMSM\nSub Total 200.00\nVAT 36.00\nBCRS 14.40\nTotal 250.40",
            200.0, 36.0, 250.40)) == 14.40

    def test_jsultana_scanned(self):
        assert _extract(_scanned(
            "J.Sultana Beverages\nSub Total 400.00\nVAT 72.00\nBCRS Deposit 70.80\nTotal 542.80",
            400.0, 72.0, 542.80)) == 70.80

    def test_nectar_scanned(self):
        assert _extract(_scanned(
            "Nectar Ltd\nNet 200.00\nVAT 36.00\nBCRS Deposit 2.40\nTotal 238.40",
            200.0, 36.0, 238.40)) == 2.40


# ── B. Negative cases — must NOT split ───────────────────────────────────────

class TestFalsePositivePrevention:
    """No BCRS label or zero/ambiguous deposit — must never split."""

    def test_arithmetic_shortcut_removed(self):
        """deposit_component_detected in reasons alone must NOT trigger split."""
        assert _extract(_digital(
            "Sub Total 200.00\nVAT 36.00\nInvoice Total 238.40",
            200.0, 36.0, 238.40,
            review_reasons="deposit_component_detected:2.40")) is None

    def test_clean_balanced_digital(self):
        assert _extract(_digital(
            "Sub Total 200.00\nVAT 36.00\nInvoice Total 236.00",
            200.0, 36.0, 236.0)) is None

    def test_dione_digital(self):
        assert _extract(_digital(
            "Dione Busuttil\nGoods\nSub Total 4210.91\nVAT 0.00\nInvoice Total 4210.91",
            4210.91, 0.0, 4210.91)) is None

    def test_dione_scanned(self):
        assert _extract(_scanned(
            "Dione Busuttil\nGoods supplied\nSub Total 4210.91\nVAT 0.00\nInvoice Total 4210.91",
            4210.91, 0.0, 4210.91)) is None

    def test_zero_deposit_digital(self):
        assert _extract(_digital(
            "Deposit Summary\nDeposit 0.00\nNet 250.00\nVAT 45.00\nTotal 295.00",
            250.0, 45.0, 295.0)) is None

    def test_zero_deposit_scanned(self):
        assert _extract(_scanned(
            "Farsons\nDeposit Summary\nDeposit 0.00\nNet 180.00\nVAT 32.40\nTotal 212.40",
            180.0, 32.40, 212.40)) is None

    def test_deposit_heading_balanced_digital(self):
        assert _extract(_digital(
            "Deposit Summary\nTotal Net 180.00\nVAT 32.40\nTotal 212.40",
            180.0, 32.40, 212.40)) is None

    def test_reconciliation_alone_no_label(self):
        assert _extract(_digital(
            "Sub Total 100.00\nVAT 18.00\nTotal 120.40",
            100.0, 18.0, 120.40)) is None

    def test_item_row_deposit_only(self):
        assert _extract({
            "page_text_raw": (
                "Item  Description  Qty  Unit  Total\n"
                "001  Glass bottle deposit  24  0.10  2.40\n"
                "Sub Total 21.60\nVAT 3.89\nInvoice Total 25.49"
            ),
            "net_amount": 21.60, "vat_amount": 3.89, "total_amount": 25.49,
        }) is None

    def test_scanned_no_di_text(self):
        """Scanned page with no di_page_text at all — must not split."""
        assert _extract({
            "totals_raw": "Net: 200.00 | VAT: 36.00 | Total: 236.00",
            "page_text_raw": "[Scanned page — no text layer — extracted via native_text+azure_di]",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 236.0,
        }) is None


# ── C. Structural guards ──────────────────────────────────────────────────────

class TestStructuralGuards:
    def test_arithmetic_shortcut_absent_from_source(self):
        src = open("app/routers/batches.py").read()
        fn = src.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "return round(float(m.group(1)), 2)" not in fn

    def test_threshold_is_20(self):
        src = open("app/routers/batches.py").read()
        fn = src.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "best_score < 20" in fn

    def test_label_line_guard_present(self):
        src = open("app/routers/batches.py").read()
        fn = src.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "has_label_line" in fn

    def test_di_page_text_in_azure_di_return(self):
        src = open("app/services/extractor.py").read()
        assert '"di_page_text":' in src
        assert 'getattr(result, "content"' in src

    def test_di_page_text_in_merge_ai_fields(self):
        src = open("app/services/extractor.py").read()
        assert '"di_page_text"' in src.split("def merge_ai_fields")[1].split("return merged")[0]

    def test_di_page_text_used_in_scanned_branch(self):
        src = open("app/services/extractor.py").read()
        assert '_di_text = extracted.get("di_page_text")' in src

    def test_split_math_present(self):
        src = open("app/routers/batches.py").read()
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src
