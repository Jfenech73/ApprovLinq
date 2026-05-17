"""Tests for app.services.classify_lines."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.classify_lines import classify_line, LineClassification

_ACCOUNTS = [
    {"account_code": "4001", "account_name": "Alcohol Purchases", "is_default": False},
    {"account_code": "4002", "account_name": "Tobacco Purchases", "is_default": False},
    {"account_code": "4003", "account_name": "Food and Beverages", "is_default": False},
    {"account_code": "9999", "account_name": "General Purchases", "is_default": True},
]


class TestUnresolved:
    def test_no_accounts_unresolved(self):
        cl = classify_line(description="Heineken 24x330ml", nominal_accounts=[])
        assert cl.classification_method == "unresolved"
        assert cl.nominal_account_code is None
        assert cl.classification_confidence == 0.0

    def test_empty_description_uses_default(self):
        cl = classify_line(description=None, line_items_raw=None, nominal_accounts=_ACCOUNTS)
        assert cl.classification_method == "default"
        assert cl.nominal_account_code == "9999"


class TestTaxonomyMatch:
    def test_heineken_matches_alcohol(self):
        cl = classify_line(
            description="Heineken 24x330ml cans",
            nominal_accounts=_ACCOUNTS,
        )
        assert cl.classification_method == "taxonomy_match"
        assert cl.nominal_account_code == "4001"
        assert cl.classification_confidence >= 0.85

    def test_rothmans_matches_tobacco(self):
        cl = classify_line(
            description="Rothmans Blue King Size",
            nominal_accounts=_ACCOUNTS,
        )
        assert cl.classification_method == "taxonomy_match"
        assert cl.nominal_account_code == "4002"

    def test_line_items_raw_checked(self):
        cl = classify_line(
            description=None,
            line_items_raw="Marlboro Red 20s x 200",
            nominal_accounts=_ACCOUNTS,
        )
        assert cl.classification_method == "taxonomy_match"
        assert cl.nominal_account_code == "4002"

    def test_brand_field_populated(self):
        cl = classify_line(
            description="Heineken 330ml",
            nominal_accounts=_ACCOUNTS,
        )
        if cl.classification_method == "taxonomy_match":
            assert cl.brand is not None


class TestSupplierRule:
    def test_supplier_rule_wins_over_taxonomy(self):
        from app.services.normalize_suppliers import SupplierNorm
        snorm = SupplierNorm(
            raw="Test Brewery",
            clean="Test Brewery",
            canonical="Test Brewery",
            match_method="alias_match",
            match_confidence=1.0,
            nominal_hint="Tobacco",  # deliberately different from taxonomy
        )
        cl = classify_line(
            description="Heineken 330ml",
            supplier_norm=snorm,
            nominal_accounts=_ACCOUNTS,
        )
        assert cl.classification_method == "supplier_rule"
        assert cl.nominal_account_code == "4002"  # Tobacco from nominal_hint


class TestHistoricalFallback:
    def test_historical_used_when_no_taxonomy(self):
        def hook():
            return "4003"

        cl = classify_line(
            description="Miscellaneous goods",
            nominal_accounts=_ACCOUNTS,
            historical_hook=hook,
        )
        assert cl.classification_method == "historical_similarity"
        assert cl.nominal_account_code == "4003"

    def test_historical_skipped_when_taxonomy_wins(self):
        def hook():
            return "9999"  # would return default

        cl = classify_line(
            description="Heineken 330ml",
            nominal_accounts=_ACCOUNTS,
            historical_hook=hook,
        )
        # Taxonomy should win before historical is even consulted
        assert cl.classification_method == "taxonomy_match"


class TestKeywordMatch:
    def test_keyword_in_description(self):
        cl = classify_line(
            description="see invoice for alcohol purchases details",
            nominal_accounts=_ACCOUNTS,
        )
        assert cl.classification_method == "keyword_match"
        assert cl.nominal_account_code == "4001"


class TestDefaultFallback:
    def test_default_account_used_last(self):
        cl = classify_line(
            description="Completely unrecognized product XYZ",
            nominal_accounts=_ACCOUNTS,
        )
        assert cl.classification_method == "default"
        assert cl.nominal_account_code == "9999"
        assert cl.classification_confidence == 0.40


class TestReturnType:
    def test_returns_line_classification(self):
        cl = classify_line(description="anything", nominal_accounts=_ACCOUNTS)
        assert isinstance(cl, LineClassification)
        assert hasattr(cl, "classification_method")
        assert hasattr(cl, "classification_confidence")
