"""Phase 1 targeted tests.

Covers:
  A. BCRS / deposit auto-split safety
  B. Arithmetic mismatch routes to review (not auto-split)
  C. Manual duplicate row action
  D. validate_invoice proportionality guard
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from app.routers.batches import _extract_bcrs_amount_from_summary
from app.services.validate_invoice import validate_invoice


# ── A. BCRS / deposit auto-split safety ─────────────────────────────────────

class TestBCRSSplitSafety:

    # --- Must split ---

    def test_explicit_bcrs_label_in_totals(self):
        """Explicit BCRS label with reconciling amount in totals block → split."""
        p = {
            'totals_raw': 'Net 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 120.40,
        }
        assert _extract_bcrs_amount_from_summary(p) == 2.40

    def test_bcrs_deposit_label_in_totals(self):
        """BCRS Deposit label → split."""
        p = {
            'totals_raw': 'Subtotal 526.22\nVAT 94.72\nBCRS Deposit 70.80\nTotal 691.74',
            'net_amount': 526.22, 'vat_amount': 94.72, 'total_amount': 691.74,
        }
        assert _extract_bcrs_amount_from_summary(p) == 70.80

    def test_deposit_in_totals_with_reconciling_arithmetic(self):
        """Plain 'Deposit' label with perfect net+vat+dep=total → split."""
        p = {
            'page_text_raw': 'Sub Total 180.00\nVAT 32.40\nDeposit 2.40\nInvoice Total 214.80',
            'net_amount': 180.0, 'vat_amount': 32.40, 'total_amount': 214.80,
        }
        assert _extract_bcrs_amount_from_summary(p) == 2.40

    def test_refundable_deposit_label(self):
        """Refundable Deposit label → split."""
        p = {
            'page_text_raw': 'Net 77.52\nVAT 10.40\nBCRS Refundable Deposit 10.80\nInvoice Total 98.72',
            'net_amount': 77.52, 'vat_amount': 10.40, 'total_amount': 98.72,
        }
        assert _extract_bcrs_amount_from_summary(p) == 10.80

    def test_deposit_summary_heading_block(self):
        """Deposit Summary heading with amount in its block → split."""
        p = {
            'page_text_raw': 'Deposit Summary\nDeposit 4.80\nNet 250.00\nVAT 45.00\nTotal 299.80',
            'net_amount': 250.0, 'vat_amount': 45.0, 'total_amount': 299.80,
        }
        assert _extract_bcrs_amount_from_summary(p) == 4.80

    # --- Must NOT split ---

    def test_no_split_on_arithmetic_mismatch_alone(self):
        """Arithmetic mismatch with no BCRS/deposit label → no split."""
        p = {
            'totals_raw': 'Net 100.00\nVAT 18.00\nTotal 120.40',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 120.40,
        }
        assert _extract_bcrs_amount_from_summary(p) is None

    def test_no_split_on_zero_value_deposit_line(self):
        """Zero-value deposit line → no split."""
        p = {
            'totals_raw': 'Net 100.00\nVAT 18.00\nBCRS Deposit 0.00\nTotal 118.00',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 118.0,
        }
        assert _extract_bcrs_amount_from_summary(p) is None

    def test_no_split_bcrs_item_row_only(self):
        """BCRS label appears only in item rows (qty×price style) → no split."""
        p = {
            'page_text_raw': 'Item Description Qty Unit Price\nBCRS PET 24 1.00 2.00\nAnother line deposit 24 0.10\n',
            'net_amount': 100.0, 'vat_amount': 23.0, 'total_amount': 123.0,
        }
        assert _extract_bcrs_amount_from_summary(p) is None

    def test_no_split_plain_deposit_no_reconciliation(self):
        """Plain 'deposit' in page text but arithmetic doesn't reconcile for that value
        AND invoice is already balanced → the deposit label in a non-summary context
        with no reconciling arithmetic should not force a split.
        Use a case where 'deposit' appears in a general description line."""
        # 'Security deposit note' is not a money line and has no amount after the label
        # Also the invoice is balanced: net+vat=total
        p = {
            'page_text_raw': 'Net 100.00\nVAT 18.00\nSecurity deposit note\nTotal 118.00',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 118.0,
        }
        # balanced AND deposit line has no monetary value → no split
        assert _extract_bcrs_amount_from_summary(p) is None

    def test_no_split_on_balanced_invoice(self):
        """Perfectly balanced invoice with no deposit label → no split."""
        p = {
            'totals_raw': 'Net 100.00\nVAT 18.00\nTotal 118.00',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 118.0,
        }
        assert _extract_bcrs_amount_from_summary(p) is None

    def test_no_split_vat_line_not_treated_as_deposit(self):
        """VAT amount line must never be selected as the BCRS candidate."""
        p = {
            'totals_raw': 'Net 100.00\nVAT 18.00\nDeposit VAT 18.00\nTotal 118.00',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 118.0,
        }
        # Even if there is a "Deposit VAT" label, 18.00 equals VAT → rejected
        result = _extract_bcrs_amount_from_summary(p)
        assert result is None

    def test_no_split_total_incl_bcrs_line(self):
        """'Total incl VAT & BCRS' combined total line → value is invoice total, not BCRS."""
        p = {
            'totals_raw': 'Net 100.00\nVAT 18.00\nTotal incl VAT & BCRS 120.40',
            'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 120.40,
        }
        assert _extract_bcrs_amount_from_summary(p) is None


# ── B. Arithmetic mismatch routes to review ──────────────────────────────────

class TestMismatchToReview:
    """Unresolved arithmetic mismatch should produce review reason codes,
    not silent acceptance or incorrect auto-split."""

    def test_large_mismatch_is_totals_mismatch(self):
        """diff=77 (77% of net) → totals_mismatch, not deposit."""
        vi = validate_invoice({'net_amount': 100.0, 'vat_amount': 23.0, 'total_amount': 200.0})
        assert vi.totals_reconciliation_status == 'totals_mismatch'
        assert 'totals_mismatch' in vi.review_reasons

    def test_small_deposit_still_detected(self):
        """A genuine small deposit diff (≤40% of net) → ok_with_deposit."""
        vi = validate_invoice({'net_amount': 100.0, 'vat_amount': 18.0, 'total_amount': 120.40})
        assert vi.totals_reconciliation_status == 'ok_with_deposit'
        assert vi.deposit_amount is not None
        assert abs(vi.deposit_amount - 2.40) < 0.01

    def test_deposit_at_40pct_boundary(self):
        """diff exactly 40% of net → ok_with_deposit (on the boundary, still deposit)."""
        # net=100, diff=40.00, diff/net=0.40 exactly → still ≤ 0.40
        vi = validate_invoice({'net_amount': 100.0, 'vat_amount': 0.0, 'total_amount': 140.00})
        assert vi.totals_reconciliation_status == 'ok_with_deposit'

    def test_deposit_just_over_40pct_boundary(self):
        """diff > 40% of net → totals_mismatch."""
        # net=100, diff=41.00 (41% of net)
        vi = validate_invoice({'net_amount': 100.0, 'vat_amount': 0.0, 'total_amount': 141.00})
        assert vi.totals_reconciliation_status == 'totals_mismatch'

    def test_mismatch_sets_review_reason(self):
        """totals_mismatch must always appear in review_reasons."""
        vi = validate_invoice({'net_amount': 200.0, 'vat_amount': 36.0, 'total_amount': 500.0})
        assert vi.totals_reconciliation_status == 'totals_mismatch'
        assert 'totals_mismatch' in vi.review_reasons
        # deposit NOT detected for large diff
        assert vi.deposit_amount is None


# ── C. Duplicate row endpoint (structural check) ─────────────────────────────

class TestDuplicateRowEndpoint:
    """Verify the duplicate_row endpoint exists in the router and has correct
    structure (route registration, handler existence).  Full DB integration
    is out of scope for unit tests — covered by the existing API contract tests."""

    def test_duplicate_row_route_registered(self):
        from app.routers.review import router
        paths = [r.path for r in router.routes]
        assert any('duplicate' in p for p in paths), (
            f"duplicate route not found in {paths}"
        )

    def test_bcrs_split_route_registered(self):
        from app.routers.review import router
        paths = [r.path for r in router.routes]
        assert any('bcrs_split' in p for p in paths), (
            f"bcrs_split route not found in {paths}"
        )

    def test_duplicate_row_handler_callable(self):
        from app.routers import review
        assert callable(getattr(review, 'duplicate_row', None))

    def test_bcrs_split_handler_callable(self):
        from app.routers import review
        assert callable(getattr(review, 'bcrs_split', None))
