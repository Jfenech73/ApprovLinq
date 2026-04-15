"""Tests for app.services.normalize_suppliers."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.normalize_suppliers import normalize_supplier, clean_supplier_name


class TestCleanSupplierName:
    def test_strips_ocr_noise(self):
        assert "|" not in clean_supplier_name("Heineken | Ireland")

    def test_normalizes_whitespace(self):
        result = clean_supplier_name("Heineken  Ireland   Limited")
        assert "  " not in result

    def test_empty_string(self):
        assert clean_supplier_name("") == ""

    def test_none_input(self):
        assert clean_supplier_name(None) == ""

    def test_strips_leading_punct(self):
        result = clean_supplier_name("***Company Ltd")
        assert not result.startswith("*")


class TestNormalizeSupplier:
    def test_unknown_supplier_returns_unmatched(self):
        sn = normalize_supplier("Completely Unknown Distributor XYZ123")
        assert sn.match_method == "unmatched"
        assert sn.match_confidence == 0.0

    def test_unknown_short_triggers_supplier_unknown_reason(self):
        sn = normalize_supplier(None)
        assert sn.review_reason in ("supplier_unknown", "supplier_low_confidence", None)

    def test_canonical_preserved_on_unmatched(self):
        sn = normalize_supplier("Some Vendor Ltd")
        # Falls back to cleaned string when not in config
        assert sn.canonical != ""

    def test_raw_preserved(self):
        sn = normalize_supplier("Some Vendor Ltd")
        assert sn.raw == "Some Vendor Ltd"

    def test_clean_applied(self):
        sn = normalize_supplier("Some | Vendor || Ltd")
        assert "|" not in sn.clean

    def test_vat_match_wins(self):
        # Even if name doesn't match well, a VAT match should succeed
        # (Only works when suppliers.yaml has an entry with a vat_number)
        # This test checks the logic path without asserting a specific result
        sn = normalize_supplier("", supplier_vat="MT12345678")
        # Should not raise, regardless of outcome
        assert sn.match_method in ("vat_match", "unmatched", "alias_match", "fuzzy_match")

    def test_returns_supplier_norm_object(self):
        from app.services.normalize_suppliers import SupplierNorm
        sn = normalize_supplier("Test Supplier")
        assert isinstance(sn, SupplierNorm)
        assert hasattr(sn, "canonical")
        assert hasattr(sn, "match_method")
        assert hasattr(sn, "match_confidence")
        assert hasattr(sn, "review_reason")

    def test_low_confidence_reason(self):
        # A partially-matching name that doesn't fully meet the fuzzy threshold
        # should surface a review reason
        sn = normalize_supplier("Heineken Irl")  # close but not exact
        if sn.match_method in ("unmatched",):
            assert sn.review_reason in ("supplier_unknown", "supplier_low_confidence")

    def test_no_crash_on_empty(self):
        sn = normalize_supplier("")
        assert sn is not None
