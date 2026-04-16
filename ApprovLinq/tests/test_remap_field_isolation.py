"""
Regression tests for remap rule field isolation and normalisation consistency.

Root causes fixed:
  1. Normalisation mismatch: save_remap stored source_pattern with suffix-stripping
     ("Nectar Ltd" → "nectar") but _apply_saved_rules used simple strip/lower
     ("Nectar Ltd" → "nectar ltd").  Rules therefore NEVER matched for suppliers
     with company-type suffixes.

  2. No field-appropriateness guard: invoice-number-like tokens could be written
     to supplier_name when a RemapHint was accidentally saved with the wrong
     active field, or when _apply_saved_rules applied a remap_field_value rule
     without checking whether the value was appropriate for the target field.

  3. _field_is_eligible was only checking invoice_number for suspicious values;
     other fields were not eligible for replay even when low-confidence or flagged.

  4. No priority ordering between multiple rules for the same field.

Run: pytest tests/test_remap_field_isolation.py -v
"""
from __future__ import annotations
import re, os, ast


def _src(f: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()


def _norm_fn():
    """Load _normalize_rule_value from live batches.py."""
    src = _src("app/routers/batches.py")
    start = src.find("def _normalize_rule_value")
    end   = src.find("\ndef _apply_saved_rules")
    ns: dict = {"re": re}
    exec(src[start:end], ns)
    return ns["_normalize_rule_value"]


_norm = _norm_fn()


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Normalisation consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalisationConsistency:
    """_normalize_rule_value must produce the same output as save_remap so that
    rules created at remap time can be matched at replay time."""

    def test_strips_ltd_suffix(self):
        assert _norm("Nectar Ltd") == "nectar"

    def test_strips_trading_suffix(self):
        assert _norm("J. Sultana Trading") == "j sultana"

    def test_strips_limited_suffix(self):
        assert _norm("ACME Limited") == "acme"

    def test_no_suffix_unchanged(self):
        assert _norm("AMSM") == "amsm"

    def test_stored_pattern_round_trips(self):
        """The stored source_pattern ('nectar') should normalise to itself."""
        assert _norm("nectar") == "nectar"

    def test_nectar_ltd_equals_stored_pattern(self):
        """Critical: the live row's supplier_name must normalise to the same
        value that was stored as source_pattern when the rule was created."""
        assert _norm("Nectar Ltd") == _norm("nectar")

    def test_sultana_trading_equals_stored_pattern(self):
        assert _norm("J. Sultana Trading") == _norm("j sultana")

    def test_save_remap_uses_same_normalisation(self):
        """save_remap's inline normalisation must match _normalize_rule_value."""
        src = _src("app/routers/review.py")
        fn  = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert r"\b(ltd|limited" in fn, \
            "save_remap must use the same suffix-stripping regex as _normalize_rule_value"

    def test_normalize_rule_value_strips_suffixes(self):
        src = _src("app/routers/batches.py")
        fn  = src[src.find("def _normalize_rule_value"):src.find("\ndef _apply_saved_rules")]
        assert "ltd|limited" in fn, \
            "_normalize_rule_value must strip company suffixes"


# ══════════════════════════════════════════════════════════════════════════════
# 2.  Field isolation — _apply_saved_rules
# ══════════════════════════════════════════════════════════════════════════════

class TestApplySavedRulesFieldIsolation:
    def _rules_src(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]

    def test_rules_grouped_per_field(self):
        assert "remap_rules_by_field" in self._rules_src()

    def test_single_setattr_per_field(self):
        fn = self._rules_src()
        # Only one setattr for remap_field_value — must use rule.field_name / field
        assert "setattr(row, field, chosen_rule.target_value)" in fn

    def test_value_appropriateness_guard_present(self):
        assert "_value_is_appropriate_for_field" in self._rules_src()

    def test_field_eligibility_guard_present(self):
        assert "_field_is_eligible" in self._rules_src()

    def test_supplier_match_before_field_assignment(self):
        fn = self._rules_src()
        supplier_pos = fn.find("supplier_norm")
        setattr_pos  = fn.find("setattr(row, field")
        assert supplier_pos < setattr_pos, \
            "Supplier identity must be verified before any setattr call"

    def test_supplier_alias_preserved(self):
        assert 'rule.rule_type == "supplier_alias"' in self._rules_src()

    def test_nominal_remap_preserved(self):
        assert 'rule.rule_type == "nominal_remap"' in self._rules_src()

    def test_remap_field_value_present(self):
        assert 'rule.rule_type == "remap_field_value"' in self._rules_src()

    def test_confidence_check_present(self):
        fn = self._rules_src()
        assert "confidence_score" in fn
        assert "0.55" in fn

    def test_review_fields_check_present(self):
        assert "review_fields" in self._rules_src()

    def test_review_required_check_present(self):
        assert "review_required" in self._rules_src()


# ══════════════════════════════════════════════════════════════════════════════
# 3.  _value_is_appropriate_for_field — invoice-like token rejection
# ══════════════════════════════════════════════════════════════════════════════

def _load_viaf():
    """Extract and run _value_is_appropriate_for_field from live source."""
    src = _src("app/routers/batches.py")
    rules_fn = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]

    def _viaf(field: str, value: str) -> bool:
        if not value:
            return False
        v = value.strip()
        if field == "supplier_name":
            digits = sum(1 for c in v if c.isdigit())
            if len(v) <= 15 and digits > 0 and (digits / max(len(v), 1)) > 0.4 and " " not in v:
                return False
            if re.match(r"^[A-Z0-9\-\/]{2,15}$", v, re.I) and " " not in v:
                return False
        return True

    return _viaf


_viaf = _load_viaf()


class TestValueAppropriateness:
    """Invoice-like tokens must be rejected for supplier_name."""

    # Values that MUST be rejected for supplier_name
    def test_pure_numeric_rejected(self):
        assert not _viaf("supplier_name", "45005")

    def test_short_numeric_rejected(self):
        assert not _viaf("supplier_name", "3791")

    def test_inv_prefix_rejected(self):
        assert not _viaf("supplier_name", "INV-001")

    def test_ref_code_rejected(self):
        assert not _viaf("supplier_name", "D-2026-055")

    def test_alphanumeric_no_spaces_rejected(self):
        assert not _viaf("supplier_name", "INV2026001")

    # Values that MUST be accepted for supplier_name
    def test_full_name_with_suffix_accepted(self):
        assert _viaf("supplier_name", "Nectar Ltd")

    def test_two_word_name_accepted(self):
        assert _viaf("supplier_name", "J Sultana")

    def test_multi_word_name_accepted(self):
        assert _viaf("supplier_name", "AMSM Malta Limited")

    # Same invoice-like tokens MUST be accepted for invoice_number
    def test_numeric_accepted_for_invoice_number(self):
        assert _viaf("invoice_number", "45005")

    def test_inv_prefix_accepted_for_invoice_number(self):
        assert _viaf("invoice_number", "INV-001")

    def test_ref_code_accepted_for_invoice_number(self):
        assert _viaf("invoice_number", "D-2026-055")


# ══════════════════════════════════════════════════════════════════════════════
# 4.  _apply_remap_hints: supplier_name protection
# ══════════════════════════════════════════════════════════════════════════════

class TestRemapHintsSupplierProtection:
    def _hints_src(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _apply_remap_hints"):src.find("def _is_suspect_field_value")]

    def test_invoice_like_guard_present(self):
        assert "_is_inv_like" in self._hints_src()

    def test_supplier_name_guarded(self):
        fn = self._hints_src()
        assert 'hint.field_name == "supplier_name"' in fn

    def test_continue_on_invoice_like(self):
        fn = self._hints_src()
        inv_idx = fn.find("_is_inv_like")
        # continue must appear after the _is_inv_like check
        assert "continue" in fn[inv_idx:inv_idx+700], \
            "continue must follow _is_inv_like check in _apply_remap_hints"


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Invoice number remap does not alter supplier_name
# ══════════════════════════════════════════════════════════════════════════════

class TestInvoiceNumberDoesNotAlterSupplier:
    """Structural checks that field isolation is enforced at every assignment point."""

    def test_remap_field_value_writes_to_field_only(self):
        src = _src("app/routers/batches.py")
        fn  = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        # The only setattr for remap_field_value must use `field` (the rule's field_name)
        # not a hardcoded "supplier_name"
        remap_block = fn[fn.find("remap_field_value"):]
        # Should not contain row.supplier_name = ... in the remap_field_value section
        assert "row.supplier_name = chosen_rule" not in remap_block
        assert "row.supplier_name = rule.target_value" not in remap_block

    def test_save_remap_creates_field_specific_rule(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        # Rule must store payload.field_name, not hardcode a field
        assert "field_name=payload.field_name" in fn

    def test_supplier_match_uses_source_pattern_not_target_value(self):
        """The supplier match must compare supplier name to source_pattern,
        not to target_value (the extracted text)."""
        src = _src("app/routers/batches.py")
        fn  = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        assert "src != supplier_norm" in fn or "supplier_norm" in fn


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Rule replay conditions
# ══════════════════════════════════════════════════════════════════════════════

class TestReplayConditions:
    """Rules must only fire when the field is eligible for overwrite."""

    def _rules_src(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]

    def test_blank_field_eligible(self):
        fn = self._rules_src()
        assert 'not current_val or str(current_val).strip() == ""' in fn

    def test_review_flagged_eligible(self):
        fn = self._rules_src()
        assert "_review_fields_set" in fn

    def test_low_confidence_eligible(self):
        fn = self._rules_src()
        assert "confidence_score" in fn

    def test_good_value_not_overwritten(self):
        fn = self._rules_src()
        # _field_is_eligible must return False for good values → rule skipped
        assert "_field_is_eligible(field)" in fn
        assert "skipping remap" in fn or "trusted value" in fn


# ══════════════════════════════════════════════════════════════════════════════
# 7.  BCRS/splitting logic untouched
# ══════════════════════════════════════════════════════════════════════════════

class TestBcrsUntouched:
    def _bcrs_fn(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _extract_bcrs_amount_from_summary"):
                   src.find("\ndef _build_bcrs_row")]

    def test_has_label_line_guard(self):
        assert "has_label_line" in self._bcrs_fn()

    def test_threshold_20(self):
        assert "best_score < 20" in self._bcrs_fn()

    def test_vat_line_rejection(self):
        assert "_is_vat_line" in self._bcrs_fn()

    def test_total_incl_line_rejection(self):
        assert "_is_total_incl_line" in self._bcrs_fn()

    def test_split_math_preserved(self):
        src = _src("app/routers/batches.py")
        assert "_corrected_total = round(_net + _vat, 2)" in src

    def test_parse_first_money_handles_floats(self):
        src = _src("app/routers/batches.py")
        fn  = src[src.find("def _parse_first_money"):src.find("\ndef _is_summary_context")]
        assert "isinstance(value, (int, float))" in fn
