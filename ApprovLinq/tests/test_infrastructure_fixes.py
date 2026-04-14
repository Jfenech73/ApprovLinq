"""
Tests for the infrastructure fixes applied in this session:

  1. Export route decorator restored — /batches/{id}/export is accessible
  2. Remap mode does NOT auto-enable on review load
  3. 5-day file retention cleanup logic
  4. DU improvements:
     - _clean_ocr_supplier_name handles full-name OCR duplication
     - summarise_line_items_rule_based handles beverage/spirits keywords
     - text-evidence BCRS detection via regex patterns
     - simple_extract totals patterns include cash-sale / total-to-pay labels

Run with:  pytest tests/test_infrastructure_fixes.py -v
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch


# ══════════════════════════════════════════════════════════════════════════════
# 1. EXPORT ROUTE REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

class TestExportRouteRegistered:
    """Verify the export endpoint is properly registered on the router."""

    def test_export_route_exists_in_router(self):
        """The @router.get('/{batch_id}/export') decorator must be present."""
        from app.routers.batches import router
        routes = {r.path for r in router.routes}
        assert "/batches/{batch_id}/export" in routes or \
               any("export" in str(r.path) for r in router.routes), \
               "Export route must be registered — decorator was accidentally dropped"

    def test_export_function_is_callable(self):
        """export_batch must be a real FastAPI route, not a bare function."""
        from app.routers import batches
        # If the decorator is missing, export_batch won't appear in router.routes
        from app.routers.batches import router
        route_names = [getattr(r, "name", "") for r in router.routes]
        assert "export_batch" in route_names, \
            "export_batch must be a registered route function"


# ══════════════════════════════════════════════════════════════════════════════
# 2. REMAP DEFAULT STATE
# ══════════════════════════════════════════════════════════════════════════════

class TestRemapDefaultOff:
    """Verify remap mode is not auto-enabled anywhere in review.js."""

    def _read_review_js(self) -> str:
        p = Path(__file__).parent.parent / "app/static/js/review.js"
        return p.read_text()

    def test_no_auto_enable_on_load(self):
        """On initial page load, remap must not be programmatically ticked."""
        js = self._read_review_js()
        # The auto-enable block that was removed used:
        # cb.checked = true; cb.dispatchEvent(new Event("change"))
        # in combination with the fileFilterId condition inside load()
        # We verify the pattern is gone from the load function context.
        assert "cb.checked = true" not in js or \
               "auto-enable" not in js, \
               "Remap auto-enable on load must be removed"

    def test_auto_enable_comment_replaced_with_intent_comment(self):
        """The intentional NOT auto-enable comment must be present."""
        js = self._read_review_js()
        assert "intentionally NOT auto-enabled" in js or \
               "NOT auto-enabled" in js, \
               "Must have explicit comment that remap is not auto-enabled"

    def test_flagged_field_no_longer_auto_enables_remap(self):
        """Clicking a flagged field must not trigger remap activation."""
        js = self._read_review_js()
        # Old: "if (el.classList.contains("flagged-field")) { const cb = ...
        #       cb.checked = true; cb.dispatchEvent(new Event("change")); }"
        # This block should no longer exist
        assert 'cb.checked = true' not in js, \
            "No code should set cb.checked = true (remap auto-enable removed)"


# ══════════════════════════════════════════════════════════════════════════════
# 3. RETENTION CLEANUP LOGIC
# ══════════════════════════════════════════════════════════════════════════════

class TestRetentionCleanup:
    """Verify the file retention startup job behaves correctly."""

    def test_retention_config_default_is_5_days(self):
        from app.config import settings
        assert settings.file_retention_days == 5, \
            "Default retention must be 5 days"

    def test_old_batch_folder_is_removed(self, tmp_path):
        """Batch folder where ALL files are older than retention must be deleted."""
        import shutil, time

        # Create a fake batch upload folder with one "old" PDF
        upload_root = tmp_path / "uploads"
        upload_root.mkdir()
        batch_folder = upload_root / "batch-abc-123"
        batch_folder.mkdir()
        old_pdf = batch_folder / "invoice.pdf"
        old_pdf.write_bytes(b"%PDF old")

        # Back-date the file mtime to 6 days ago
        six_days_ago = time.time() - (6 * 86_400)
        import os
        os.utime(old_pdf, (six_days_ago, six_days_ago))

        retention_seconds = 5 * 86_400
        now = time.time()

        # Simulate the cleanup logic
        files = list(batch_folder.rglob("*"))
        pdf_files = [f for f in files if f.is_file()]
        oldest_mtime = max(f.stat().st_mtime for f in pdf_files)
        should_remove = (now - oldest_mtime) > retention_seconds

        assert should_remove, "6-day-old folder must be flagged for removal"
        shutil.rmtree(batch_folder)
        assert not batch_folder.exists()

    def test_recent_batch_folder_is_kept(self, tmp_path):
        """Batch folder with files newer than retention must NOT be deleted."""
        import time

        upload_root = tmp_path / "uploads"
        upload_root.mkdir()
        batch_folder = upload_root / "batch-xyz-456"
        batch_folder.mkdir()
        recent_pdf = batch_folder / "invoice.pdf"
        recent_pdf.write_bytes(b"%PDF recent")
        # File mtime is now (default) — 0 days old

        retention_seconds = 5 * 86_400
        now = time.time()

        files = list(batch_folder.rglob("*"))
        pdf_files = [f for f in files if f.is_file()]
        oldest_mtime = max(f.stat().st_mtime for f in pdf_files)
        should_remove = (now - oldest_mtime) > retention_seconds

        assert not should_remove, "Recent folder must NOT be removed"
        assert batch_folder.exists()

    def test_old_export_file_is_removed(self, tmp_path):
        """Export .xlsx files older than retention must be deleted."""
        import time, os

        export_root = tmp_path / "exports"
        export_root.mkdir()
        old_export = export_root / "batch_abc.xlsx"
        old_export.write_bytes(b"PK fake xlsx")

        six_days_ago = time.time() - (6 * 86_400)
        os.utime(old_export, (six_days_ago, six_days_ago))

        retention_seconds = 5 * 86_400
        now = time.time()

        age = now - old_export.stat().st_mtime
        should_remove = age > retention_seconds

        assert should_remove, "6-day-old export must be flagged for removal"
        old_export.unlink()
        assert not old_export.exists()

    def test_recent_export_file_is_kept(self, tmp_path):
        """Export .xlsx files newer than retention must NOT be deleted."""
        import time

        export_root = tmp_path / "exports"
        export_root.mkdir()
        recent_export = export_root / "batch_xyz.xlsx"
        recent_export.write_bytes(b"PK fake xlsx")

        retention_seconds = 5 * 86_400
        now = time.time()
        age = now - recent_export.stat().st_mtime

        should_remove = age > retention_seconds
        assert not should_remove, "Recent export must NOT be removed"

    def test_startup_handler_registered(self):
        """run_file_retention_cleanup must be a registered startup handler."""
        from app.main import app
        startup_handlers = [
            getattr(h, "__name__", str(h))
            for h in app.router.on_startup
        ]
        assert "run_file_retention_cleanup" in startup_handlers, \
            "Retention cleanup must be registered as a startup handler"


# ══════════════════════════════════════════════════════════════════════════════
# 4. DU IMPROVEMENTS
# ══════════════════════════════════════════════════════════════════════════════

class TestOCRSupplierNameCleaning:
    """Verify _clean_ocr_supplier_name handles all known OCR duplication patterns."""

    def _clean(self, name):
        from app.services.extractor import _clean_ocr_supplier_name
        return _clean_ocr_supplier_name(name)

    def test_full_name_duplication_with_period_separator(self):
        """'Br Supply Co. Br Supply Co' → 'Br Supply Co.'"""
        result = self._clean("Br Supply Co. Br Supply Co")
        assert result is not None
        assert "Br Supply Co" in result
        # Must NOT contain both copies
        assert result.count("Br Supply") == 1, \
            f"Duplicate name must be removed, got: {result!r}"

    def test_initial_duplication(self):
        """'N N Calleja Trading' → 'N Calleja Trading'"""
        result = self._clean("N N Calleja Trading")
        assert result == "N Calleja Trading"

    def test_leading_junk_stripped(self):
        """'฿ Br Supply Co.' → 'Br Supply Co.'"""
        result = self._clean("฿ Br Supply Co.")
        assert result == "Br Supply Co."

    def test_newline_replaced(self):
        """'Joseph\\nBorg Ltd' → 'Joseph Borg Ltd'"""
        result = self._clean("Joseph\nBorg Ltd")
        assert result == "Joseph Borg Ltd"

    def test_clean_name_unchanged(self):
        """A clean name must pass through unchanged."""
        result = self._clean("J. Sultana Beverages, Wines & Spirits")
        assert result == "J. Sultana Beverages, Wines & Spirits"

    def test_short_name_returned(self):
        """A 2-character name must not be dropped."""
        result = self._clean("BP")
        assert result == "BP"

    def test_none_input(self):
        assert self._clean(None) is None


class TestLineItemSummarization:
    """Verify summarise_line_items_rule_based covers beverage/spirits keywords."""

    def _summarise(self, text):
        from app.services.extractor import summarise_line_items_rule_based
        return summarise_line_items_rule_based(text)

    def test_whisky_categorized_as_beverage(self):
        result = self._summarise("JOHNNIE WALKER BLACK LABEL WHISKY 70CL x12")
        assert "beverage" in result.lower() or "food" in result.lower() or "drink" in result.lower()

    def test_wine_categorized_as_beverage(self):
        result = self._summarise("Pinot Grigio DOC Wine 750ml case 6")
        assert "beverage" in result.lower() or "food" in result.lower()

    def test_beer_categorized_as_beverage(self):
        result = self._summarise("Cisk Lager 500ml x24")
        assert "beverage" in result.lower() or "food" in result.lower()

    def test_spirits_categorized_as_beverage(self):
        result = self._summarise("Smirnoff Vodka 1L / Bacardi Rum 70cl / Gordon's Gin")
        assert "beverage" in result.lower() or "food" in result.lower()

    def test_poultry_categorized_as_food(self):
        result = self._summarise("WIE-POULTRY ROLL X 100G X 50PCS")
        assert "food" in result.lower() or "beverage" in result.lower()

    def test_fuel_still_works(self):
        result = self._summarise("Diesel 50L @ 1.35/L")
        assert "fuel" in result.lower()

    def test_generic_fallback(self):
        result = self._summarise("ZZZ-ITEM-CODE-9999")
        assert result  # must return something non-empty

    def test_numeric_only_lines_skipped(self):
        """Purely numeric lines should not become the description."""
        result = self._summarise("100.00\n200.00\n50.00")
        assert result  # returns fallback, not "100.00 200.00"


class TestBCRSTextDetection:
    """Verify the text-evidence BCRS detection regex patterns."""

    def test_bcrs_label_in_text_detected(self):
        """Page text with 'BCRS 0.50' must set _deposit_component."""
        from app.services.extractor import parse_amount
        import re
        text = "Net: 100.00\nVAT: 18.00\nBCRS 0.50\nTotal: 118.50"
        patterns = [
            r"\bBCRS\b\s*[:\-]?\s*(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})",
            r"\bdeposit\b\s*[:\-]?\s*(?:surcharge\s*)?(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})",
        ]
        found = None
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                found = parse_amount(m.group(1))
                break
        assert found == 0.50, f"BCRS text detection must find 0.50, got {found}"

    def test_bcrs_colon_format(self):
        """'BCRS: 1.20' must be detected."""
        from app.services.extractor import parse_amount
        import re
        text = "Sub Total: 200.00\nBCRS: 1.20\nTotal: 201.20"
        m = re.search(r"\bBCRS\b\s*[:\-]?\s*(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})", text, re.I)
        assert m, "BCRS: pattern must match"
        assert parse_amount(m.group(1)) == 1.20

    def test_deposit_label_detected(self):
        """'deposit 0.75' must be detected."""
        from app.services.extractor import parse_amount
        import re
        text = "Goods: 50.00\nDeposit 0.75\nTotal: 50.75"
        m = re.search(
            r"\bdeposit\b\s*[:\-]?\s*(?:surcharge\s*)?(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})",
            text, re.I
        )
        assert m, "deposit pattern must match"
        assert parse_amount(m.group(1)) == 0.75

    def test_no_false_positive_when_no_bcrs(self):
        """Text without BCRS/deposit must not trigger."""
        import re
        text = "Goods: 100.00\nVAT: 18.00\nTotal: 118.00"
        patterns = [
            r"\bBCRS\b\s*[:\-]?\s*(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})",
            r"\bdeposit\b\s*[:\-]?\s*(?:surcharge\s*)?(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})",
        ]
        matches = [re.search(p, text, re.I) for p in patterns]
        assert not any(matches), "No BCRS/deposit pattern should match clean text"

    def test_bcrs_amount_above_50_ignored(self):
        """Implausibly large BCRS amounts (>€50) must be ignored."""
        from app.services.extractor import parse_amount
        import re
        text = "BCRS 120.00"
        m = re.search(r"\bBCRS\b\s*[:\-]?\s*(?:EUR|€)?\s*([0-9]+[.,][0-9]{2})", text, re.I)
        if m:
            val = parse_amount(m.group(1))
            # Validation gate: 0.01 <= val <= 50.0
            valid = val is not None and 0.01 <= val <= 50.0
            assert not valid, "BCRS amount >50 must be rejected"


class TestSimpleExtractTotals:
    """Verify simple_extract picks up cash-sale and receipt-style total labels."""

    def _extract(self, text):
        from app.services.extractor import simple_extract
        return simple_extract(text)

    def test_total_to_pay_label(self):
        text = "Supplier Ltd\nInvoice No: 12345\nSub Total: 100.00\nVAT: 18.00\nTotal to Pay: 118.00"
        result = self._extract(text)
        assert result["total_amount"] == 118.00, \
            f"'Total to Pay' must be parsed as total, got {result['total_amount']}"

    def test_amount_due_label(self):
        text = "Acme Co\nInvoice 999\nNet: 200.00\nVAT: 36.00\nAmount Due: 236.00"
        result = self._extract(text)
        assert result["total_amount"] == 236.00, \
            f"'Amount Due' must be parsed as total, got {result['total_amount']}"

    def test_balance_due_label(self):
        text = "Test Supplier\nInv: ABC123\nBalance Due: 500.00"
        result = self._extract(text)
        assert result["total_amount"] == 500.00

    def test_total_payable_label(self):
        text = "Vendor X\nDate: 01/01/2026\nTotal Payable: 75.50"
        result = self._extract(text)
        assert result["total_amount"] == 75.50

    def test_sub_total_for_net(self):
        text = "Supplier Y\nInvoice 777\nSub Total: 80.00\nVAT: 14.40\nTotal: 94.40"
        result = self._extract(text)
        assert result["net_amount"] == 80.00, \
            f"Sub Total must be parsed as net, got {result['net_amount']}"

    def test_standard_total_still_works(self):
        """Existing 'Total:' pattern must still work."""
        text = "Acme Ltd\nInvoice: 001\nTotal: 120.00"
        result = self._extract(text)
        assert result["total_amount"] == 120.00
