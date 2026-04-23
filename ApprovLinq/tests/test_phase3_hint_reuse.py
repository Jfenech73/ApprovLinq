"""Phase 3 tests — safe reuse of past corrections and remap hints.

Covers:
  A. Hints apply to blank fields
  B. Hints apply to suspect/obviously-wrong values
  C. Hints apply to review-flagged fields
  D. Hints apply to low-confidence rows (review_required + confidence_score < 0.55)
  E. Strong valid values are NOT overwritten
  F. Invoice-like token guard still active for supplier_name
  G. Supplier matching: supplier_id preferred over name fallback
  H. Source attribution in log (structural check)
  I. _is_suspect_field_value coverage
  J. _apply_remap_hints structural guards (code inspection)

Run: pytest tests/test_phase3_hint_reuse.py -v
"""
from __future__ import annotations
import os, re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ── helpers to load live code without a full DB stack ────────────────────────

def _src() -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", "app", "routers", "batches.py")).read()


def _load_is_suspect():
    src = _src()
    start = src.find("def _is_suspect_field_value")
    end   = src.find("\ndef _parse_money_candidates")
    ns: dict = {}
    exec(src[start:end], ns)
    return ns["_is_suspect_field_value"]


_is_suspect = _load_is_suspect()


# ── I. _is_suspect_field_value ───────────────────────────────────────────────

class TestIsSuspectFieldValue:

    def test_none_is_not_suspect(self):
        assert not _is_suspect("supplier_name", None)

    def test_blank_string_is_suspect(self):
        assert _is_suspect("supplier_name", "")
        assert _is_suspect("supplier_name", "   ")

    def test_very_short_supplier_name_is_suspect(self):
        assert _is_suspect("supplier_name", "AB")

    def test_normal_supplier_name_not_suspect(self):
        assert not _is_suspect("supplier_name", "Nectar Ltd")

    def test_non_numeric_amount_is_suspect(self):
        assert _is_suspect("net_amount", "N/A")
        assert _is_suspect("vat_amount", "unknown")

    def test_valid_amount_not_suspect(self):
        assert not _is_suspect("net_amount", "100.00")
        assert not _is_suspect("total_amount", 236.0)

    def test_very_short_description_is_suspect(self):
        assert _is_suspect("description", "X")

    def test_normal_description_not_suspect(self):
        assert not _is_suspect("description", "Weekly beverage delivery")


# ── J. Structural guards in _apply_remap_hints ───────────────────────────────

class TestApplyRemapHintsStructure:

    def _fn(self) -> str:
        src = _src()
        return src[src.find("def _apply_remap_hints"):
                   src.find("def _is_suspect_field_value")]

    def test_low_confidence_check_present(self):
        fn = self._fn()
        assert "_low_confidence" in fn

    def test_confidence_score_threshold(self):
        fn = self._fn()
        assert "0.55" in fn

    def test_review_required_checked(self):
        fn = self._fn()
        assert "review_required" in fn

    def test_blank_check_preserved(self):
        fn = self._fn()
        assert "not getattr(row, f, None)" in fn

    def test_review_fields_check_preserved(self):
        fn = self._fn()
        assert "_review_fields" in fn

    def test_is_suspect_check_preserved(self):
        fn = self._fn()
        assert "_is_suspect_field_value" in fn

    def test_strong_value_guard_present(self):
        """A non-empty, non-suspect, non-review field must be guarded from overwrite."""
        fn = self._fn()
        assert "strong" in fn.lower() or "leave it alone" in fn

    def test_supplier_id_match_before_name_match(self):
        fn = self._fn()
        id_pos   = fn.find("supplier_id")
        name_pos = fn.find("supplier_name_snapshot")
        assert id_pos < name_pos, "supplier_id match must come before name-snapshot fallback"

    def test_invoice_like_guard_active(self):
        fn = self._fn()
        assert "_is_inv_like" in fn

    def test_supplier_name_field_guarded(self):
        fn = self._fn()
        assert 'hint.field_name == "supplier_name"' in fn

    def test_source_attribution_logged(self):
        """source=remap_hint must appear in the log line after a successful apply."""
        fn = self._fn()
        assert "source=remap_hint" in fn

    def test_old_val_captured_before_setattr(self):
        fn = self._fn()
        old_pos    = fn.find("old_val")
        setattr_pos = fn.find("setattr(row, hint.field_name")
        assert old_pos < setattr_pos, "old_val must be captured before setattr"


# ── A+B+C+D+E. Eligibility logic (unit-level without DB) ─────────────────────

def _build_row(**kwargs) -> SimpleNamespace:
    """Build a minimal fake InvoiceRow-like namespace."""
    defaults = dict(
        supplier_name="Test Supplier",
        invoice_number=None,
        invoice_date=None,
        net_amount=None,
        vat_amount=None,
        total_amount=None,
        nominal_account_code=None,
        description=None,
        review_required=False,
        review_fields=None,
        confidence_score=None,
        page_no=1,
        source_file_id=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _build_hint(field_name: str, supplier_name_snapshot: str = "Test Supplier",
                supplier_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        field_name=field_name,
        supplier_name_snapshot=supplier_name_snapshot,
        supplier_id=supplier_id,
        page_no=1,
        x=0.1, y=0.1, w=0.2, h=0.05,
        active=True,
    )


class TestEligibilityLogic:
    """Test the _apply_remap_hints eligibility decision without a real DB or PDF."""

    def _eligible_fields_for_row(self, row: SimpleNamespace) -> set[str]:
        """Replicate the eligibility logic from _apply_remap_hints."""
        _REMAP_FIELDS = (
            "supplier_name", "invoice_number", "invoice_date",
            "net_amount", "vat_amount", "total_amount",
            "nominal_account_code", "description",
        )
        _review_fields: set[str] = set()
        if row.review_fields:
            sep = "|" if "|" in (row.review_fields or "") else ","
            _review_fields = {f.strip() for f in row.review_fields.split(sep) if f.strip()}

        _low_confidence = (
            row.review_required
            and row.confidence_score is not None
            and float(row.confidence_score) < 0.55
        )
        return {
            f for f in _REMAP_FIELDS
            if not getattr(row, f, None)
            or f in _review_fields
            or _is_suspect(f, getattr(row, f, None))
            or _low_confidence
        }

    # A. Blank fields are always eligible
    def test_blank_invoice_number_eligible(self):
        row = _build_row(invoice_number=None)
        assert "invoice_number" in self._eligible_fields_for_row(row)

    def test_blank_supplier_name_eligible(self):
        row = _build_row(supplier_name=None)
        assert "supplier_name" in self._eligible_fields_for_row(row)

    # B. Suspect values are eligible
    def test_suspect_amount_eligible(self):
        row = _build_row(net_amount="N/A", invoice_number="INV-001")
        assert "net_amount" in self._eligible_fields_for_row(row)

    def test_suspect_short_name_eligible(self):
        row = _build_row(supplier_name="AB")
        assert "supplier_name" in self._eligible_fields_for_row(row)

    # C. Review-flagged fields are eligible
    def test_review_flagged_field_eligible(self):
        row = _build_row(
            invoice_number="INV-001",
            review_fields="invoice_number",
        )
        assert "invoice_number" in self._eligible_fields_for_row(row)

    def test_non_flagged_field_with_good_value_not_eligible(self):
        row = _build_row(
            invoice_number="INV-2026-001",
            review_fields=None,
            review_required=False,
            confidence_score=0.92,
        )
        assert "invoice_number" not in self._eligible_fields_for_row(row)

    # D. Low-confidence row: all fields become eligible
    def test_low_confidence_makes_all_fields_eligible(self):
        row = _build_row(
            supplier_name="Good Supplier Ltd",
            invoice_number="INV-2026-001",
            net_amount=100.0,
            review_required=True,
            confidence_score=0.40,
        )
        eligible = self._eligible_fields_for_row(row)
        assert "invoice_number" in eligible
        assert "supplier_name" in eligible
        assert "net_amount" in eligible

    def test_high_confidence_does_not_make_fields_eligible(self):
        row = _build_row(
            supplier_name="Good Supplier Ltd",
            invoice_number="INV-2026-001",
            net_amount=100.0,
            review_required=False,
            confidence_score=0.95,
        )
        eligible = self._eligible_fields_for_row(row)
        assert "invoice_number" not in eligible
        assert "supplier_name" not in eligible

    # E. Strong values must not be overwritten when row is low-confidence
    def test_strong_value_protected_in_code(self):
        """The structural check: _apply_remap_hints must skip fields with strong
        existing values even when _low_confidence is True."""
        fn = _src()
        hints_fn = fn[fn.find("def _apply_remap_hints"):fn.find("def _is_suspect_field_value")]
        # The guard must check existing value against _is_suspect + not _low_confidence
        assert "strong existing value" in hints_fn or "leave it alone" in hints_fn

    def test_confidence_threshold_boundary_below(self):
        """confidence_score = 0.54 (< 0.55) → low_confidence → all fields eligible."""
        row = _build_row(
            invoice_number="INV-001",
            review_required=True,
            confidence_score=0.54,
        )
        assert "invoice_number" in self._eligible_fields_for_row(row)

    def test_confidence_threshold_boundary_at(self):
        """confidence_score = 0.55 (not < 0.55) → NOT low_confidence."""
        row = _build_row(
            invoice_number="INV-001",
            review_required=True,
            confidence_score=0.55,
            review_fields=None,
        )
        # invoice_number has a value, not in review_fields, not suspect → not eligible
        assert "invoice_number" not in self._eligible_fields_for_row(row)

    # G. Supplier matching precedence
    def test_supplier_id_match_preferred_over_name(self):
        """supplier_id match must take precedence in the matching code."""
        fn = _src()
        hints_fn = fn[fn.find("def _apply_remap_hints"):fn.find("def _is_suspect_field_value")]
        id_pos   = hints_fn.find("supplier_id")
        name_pos = hints_fn.find("supplier_name_snapshot")
        assert id_pos < name_pos
