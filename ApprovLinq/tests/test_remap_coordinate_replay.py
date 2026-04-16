"""
Regression tests for remap coordinate-replay semantics.

Root cause fixed:
    _apply_saved_rules for remap_field_value rules was doing:
        setattr(row, field, chosen_rule.target_value)
    This assigned the STORED value from the first invoice to every subsequent
    invoice, causing all Nectar invoices to share invoice number "45005".

Correct behaviour:
    - remap_field_value rules store WHERE to read the field (coordinates via RemapHint)
    - target_value in the rule is a debug/example reference only
    - replay MUST re-read the corresponding RemapHint coordinates against the
      CURRENT invoice's PDF every time
    - text_correction rules are the only type that may reuse target_value, and
      only when the current scanned text matches source_pattern

Run: pytest tests/test_remap_coordinate_replay.py -v
"""
from __future__ import annotations
import ast, os, re


def _src(f: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()


def _rules_fn() -> str:
    src = _src("app/routers/batches.py")
    return src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]


# ══════════════════════════════════════════════════════════════════════════════
# 1.  The primary fix: remap_field_value must re-read, not replay stored value
# ══════════════════════════════════════════════════════════════════════════════

class TestCoordinateReplaySemantics:
    """The core invariant: coordinate rules must NEVER assign target_value directly."""

    def test_remap_field_value_does_not_assign_target_value_directly(self):
        fn = _rules_fn()
        # Find the remap_field_value handling block
        rv_idx = fn.find('"remap_field_value"')
        assert rv_idx > 0, "remap_field_value must be handled"
        block = fn[rv_idx:]
        # In the remap_field_value branch there must be NO direct assignment
        # of chosen_rule.target_value or rule.target_value to the row field.
        # The only setattr in this branch must use fresh_text.
        assert "setattr(row, field, chosen_rule.target_value)" not in block, \
            "remap_field_value must NOT assign target_value directly to the row"
        assert "setattr(row, field, rule.target_value)" not in block, \
            "remap_field_value must NOT assign rule.target_value directly to the row"

    def test_remap_field_value_calls_read_region_text(self):
        fn = _rules_fn()
        rv_idx = fn.find('"remap_field_value"')
        block  = fn[rv_idx:]
        assert "_read_region_text(" in block, \
            "remap_field_value replay must call _read_region_text to re-read the current invoice"

    def test_remap_field_value_uses_fresh_text_not_stored_value(self):
        fn = _rules_fn()
        rv_idx = fn.find('"remap_field_value"')
        block  = fn[rv_idx:]
        # The setattr in the remap_field_value branch must use fresh_text
        fresh_setattr_idx = block.find("setattr(row, field, fresh_text)")
        assert fresh_setattr_idx > 0, \
            "remap_field_value must assign fresh_text (from current PDF re-read) to the field"

    def test_target_value_explicitly_documented_as_not_for_replay(self):
        fn = _rules_fn()
        # The comment must make clear that target_value is NOT used for replay
        assert "NOT assign" in fn or "NOT used" in fn or "NOT reused" in fn, \
            "Code comment must explicitly state that target_value is not used for coordinate replay"

    def test_stored_example_not_assigned_warning_logged(self):
        fn = _rules_fn()
        # When no RemapHint is found, a warning must be logged and no assignment made
        assert "logger.warning(" in fn[fn.find('"remap_field_value"'):], \
            "A warning must be logged when no RemapHint is found (not silently ignored)"
        # And the warning must NOT fallback to assigning stored value
        warning_block = fn[fn.find("logger.warning("):fn.find("logger.warning(")+400]
        assert "setattr" not in warning_block, \
            "The warning block must not contain a setattr fallback to stored value"


# ══════════════════════════════════════════════════════════════════════════════
# 2.  text_correction is correctly separated from coordinate rules
# ══════════════════════════════════════════════════════════════════════════════

class TestTextCorrectionSeparation:
    """text_correction rules may reuse target_value — but only when scanned text matches."""

    def test_text_correction_rule_type_handled(self):
        fn = _rules_fn()
        assert '"text_correction"' in fn, \
            "text_correction rule type must be explicitly handled"

    def test_text_correction_checks_scanned_text_match(self):
        fn = _rules_fn()
        # Find the actual handler (inside the for-loop branch)
        tc_idx = fn.find('if chosen_rule.rule_type == "text_correction"')
        assert tc_idx > 0, "text_correction handler must exist"
        block  = fn[tc_idx:tc_idx + 800]
        assert "source_pattern" in block, \
            "text_correction must check source_pattern before assigning target_value"
        assert "current_norm" in block or "current_raw" in block, \
            "text_correction must read current field value for comparison"

    def test_text_correction_assigns_target_value_only_when_matched(self):
        fn = _rules_fn()
        tc_idx = fn.find('if chosen_rule.rule_type == "text_correction"')
        assert tc_idx > 0, "text_correction handler must exist"
        block  = fn[tc_idx:tc_idx + 1000]
        setattr_idx = block.find("setattr(row, field, val)")
        assert setattr_idx > 0, "text_correction must use setattr with val"
        match_idx = block.find("current_norm")
        assert match_idx < setattr_idx, \
            "text_correction must check match before setattr"

    def test_text_correction_does_not_call_read_region_text(self):
        fn = _rules_fn()
        tc_idx = fn.find('if chosen_rule.rule_type == "text_correction"')
        rv_idx = fn.find('if chosen_rule.rule_type != "remap_field_value"', tc_idx)
        tc_block = fn[tc_idx:rv_idx] if rv_idx > tc_idx else fn[tc_idx:tc_idx + 1000]
        assert "_read_region_text(" not in tc_block, \
            "text_correction must not call _read_region_text"


# ══════════════════════════════════════════════════════════════════════════════
# 3.  RemapHint lookup before any coordinate read
# ══════════════════════════════════════════════════════════════════════════════

class TestRemapHintLookup:
    def test_remaphis_looked_up_by_supplier_and_field(self):
        fn = _rules_fn()
        # Must query RemapHint filtered by field_name
        assert "RemapHint.field_name == field" in fn or \
               "RemapHint.field_name ==" in fn

    def test_hint_supplier_validated_before_use(self):
        fn = _rules_fn()
        # Must check hint supplier matches row supplier
        assert "hint_norm" in fn or "supplier_name_snapshot" in fn

    def test_hint_none_path_skips_assignment(self):
        fn = _rules_fn()
        # Find the final "hint is None" guard (second occurrence — the real guard)
        # The first occurrence is the page-no fallback; the second is the final check
        positions = []
        idx = 0
        while True:
            idx = fn.find("hint is None", idx)
            if idx < 0: break
            positions.append(idx)
            idx += 1
        assert len(positions) >= 2, "Must have at least 2 'hint is None' checks"
        # Use the last occurrence which is the final guard before the warning
        final_idx = positions[-1]
        block = fn[final_idx:final_idx + 400]
        assert "logger.warning(" in block or "continue" in block, \
            "When no RemapHint found, must warn and/or continue without assigning"

    def test_empty_region_does_not_fallback_to_stored_value(self):
        fn = _rules_fn()
        # Search within the remap_field_value handler block only
        rv_handler = fn.find('if chosen_rule.rule_type != "remap_field_value"')
        rv_block = fn[rv_handler:] if rv_handler > 0 else fn
        empty_idx = rv_block.find("not fresh_text")
        assert empty_idx > 0, "remap_field_value must check if fresh_text is empty"
        block = rv_block[empty_idx:empty_idx + 400]
        assert "continue" in block, \
            "When region is empty, must continue without assigning stored value"
        assert "target_value" not in block, \
            "When region is empty, must NOT fall back to target_value"


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Two invoices from same supplier keep different numbers
# ══════════════════════════════════════════════════════════════════════════════

class TestTwoInvoicesRemainDifferent:
    """Structural proof that the fix prevents value sharing between invoices."""

    def test_no_unconditional_target_value_assignment(self):
        """There must be no code path that unconditionally assigns target_value
        to a field for remap_field_value rules."""
        fn = _rules_fn()
        # Find all setattr calls
        idx = 0
        while True:
            idx = fn.find("setattr(row", idx)
            if idx < 0:
                break
            line_start = fn.rfind("\n", 0, idx) + 1
            line = fn[line_start:fn.find("\n", idx)]
            # If this setattr uses target_value, it must be inside a text_correction branch
            if "target_value" in line or "chosen_rule.target_value" in line:
                # Check that we're inside text_correction, not remap_field_value
                preceding = fn[max(0, idx-800):idx]
                assert '"remap_field_value"' not in preceding.split('"text_correction"')[-1], \
                    f"Found direct target_value assignment that may be in remap_field_value branch: {line}"
            idx += 1

    def test_fresh_text_comes_from_current_pdf_read(self):
        fn = _rules_fn()
        # The fresh_text variable must be assigned from _read_region_text
        fresh_assign_idx = fn.find("fresh_text = _read_region_text(")
        if fresh_assign_idx < 0:
            # May be assigned via intermediate variable
            fresh_assign_idx = fn.find("_read_region_text(")
        assert fresh_assign_idx > 0, \
            "fresh_text must be assigned from _read_region_text call"

    def test_coordinate_logging_shows_fresh_vs_stored(self):
        fn = _rules_fn()
        # Log must show both fresh_text and that stored value was NOT used
        assert "fresh_text" in fn[fn.find("coordinate-replay"):] or \
               "fresh=" in fn[fn.find("coordinate-replay"):]
        assert "NOT used" in fn or "NOT assigned" in fn or "NOT reused" in fn


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Normalisation and field isolation still work
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalisationAndIsolation:
    def test_normalisation_consistent(self):
        src = _src("app/routers/batches.py")
        norm_end = src.find("\ndef _apply_saved_rules")
        ns: dict = {"re": re}
        exec(src[src.find("def _normalize_rule_value"):norm_end], ns)
        _norm = ns["_normalize_rule_value"]
        assert _norm("Nectar Ltd") == "nectar"
        assert _norm("J. Sultana Trading") == "j sultana"
        assert _norm("Nectar Ltd") == _norm("nectar")

    def test_supplier_name_not_contaminated(self):
        fn = _rules_fn()
        # No direct assignment of invoice-number-like text to supplier_name
        assert "_value_is_appropriate_for_field" in fn


# ══════════════════════════════════════════════════════════════════════════════
# 6.  BCRS / split logic untouched
# ══════════════════════════════════════════════════════════════════════════════

class TestBcrsUntouched:
    def _bcrs(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _extract_bcrs_amount_from_summary"):
                   src.find("\ndef _build_bcrs_row")]

    def test_has_label_line_guard(self):
        assert "has_label_line" in self._bcrs()

    def test_threshold_20(self):
        assert "best_score < 20" in self._bcrs()

    def test_vat_line_rejection(self):
        assert "_is_vat_line" in self._bcrs()

    def test_total_incl_rejection(self):
        assert "_is_total_incl_line" in self._bcrs()

    def test_split_math_preserved(self):
        src = _src("app/routers/batches.py")
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src

    def test_parse_first_money_handles_floats(self):
        src = _src("app/routers/batches.py")
        fn  = src[src.find("def _parse_first_money"):src.find("\ndef _is_summary_context")]
        assert "isinstance(value, (int, float))" in fn

    def test_syntax_ok(self):
        ast.parse(_src("app/routers/batches.py"))
        ast.parse(_src("app/routers/review.py"))
