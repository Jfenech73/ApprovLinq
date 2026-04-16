"""Tests for app.services.review_engine."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.review_engine import compute_review_decision, ReviewDecision


_CLEAN = {
    "supplier_name": "Test Supplier Ltd",
    "invoice_number": "INV-001",
    "invoice_date": "2024-03-15",
    "total_amount": 123.0,
    "net_amount": 100.0,
    "vat_amount": 23.0,
}


class TestCleanInvoice:
    def test_clean_invoice_not_required(self):
        rd = compute_review_decision(_CLEAN, confidence=0.92, page_quality=0.95)
        assert rd.review_required is False
        assert rd.review_priority is None
        assert rd.review_reasons == []
        assert rd.validation_status == "ok"

    def test_auto_approved_false_for_clean(self):
        rd = compute_review_decision(_CLEAN, confidence=0.92, page_quality=0.95)
        assert rd.auto_approved is False


class TestMissingCriticalFields:
    def test_no_supplier_triggers_high(self):
        d = {**_CLEAN, "supplier_name": None}
        rd = compute_review_decision(d, confidence=0.90)
        assert rd.review_required is True
        assert rd.review_priority == "high"
        assert "no_supplier" in rd.review_reasons

    def test_no_amount_triggers_high(self):
        d = {**_CLEAN, "total_amount": None}
        rd = compute_review_decision(d, confidence=0.90)
        assert rd.review_required is True
        assert "no_amount" in rd.review_reasons

    def test_missing_invoice_number(self):
        d = {**_CLEAN, "invoice_number": None}
        rd = compute_review_decision(d, confidence=0.90)
        assert "invoice_number_missing" in rd.review_reasons

    def test_both_missing_is_high(self):
        rd = compute_review_decision({}, confidence=0.0)
        assert rd.review_priority == "high"
        assert rd.review_required is True


class TestSupplierReasons:
    def test_supplier_low_confidence_in_medium(self):
        rd = compute_review_decision(
            _CLEAN,
            supplier_reasons=["supplier_low_confidence"],
            confidence=0.85,
        )
        assert "supplier_low_confidence" in rd.review_reasons
        assert rd.review_priority == "medium"
        assert rd.review_required is False

    def test_supplier_unknown_triggers_high(self):
        rd = compute_review_decision(
            _CLEAN,
            supplier_reasons=["supplier_unknown"],
            confidence=0.85,
        )
        assert rd.review_priority == "high"
        assert rd.review_required is True


class TestValidationReasons:
    def test_totals_mismatch_triggers_high(self):
        rd = compute_review_decision(
            _CLEAN,
            supplier_reasons=["totals_mismatch"],
            confidence=0.90,
        )
        assert rd.review_required is True
        assert rd.review_priority == "high"

    def test_deposit_detected_is_low(self):
        rd = compute_review_decision(
            _CLEAN,
            supplier_reasons=["deposit_component_detected:1.00"],
            confidence=0.90,
        )
        assert rd.review_priority == "low"
        assert rd.review_required is False


class TestConfidenceAndQuality:
    def test_very_low_confidence_adds_reason(self):
        rd = compute_review_decision(_CLEAN, confidence=0.4, page_quality=0.9)
        assert "low_confidence" in rd.review_reasons

    def test_low_page_quality_adds_reason(self):
        rd = compute_review_decision(_CLEAN, confidence=0.9, page_quality=0.2)
        assert "page_quality_low" in rd.review_reasons

    def test_both_issues_medium_priority(self):
        rd = compute_review_decision(_CLEAN, confidence=0.4, page_quality=0.2)
        assert rd.review_priority == "medium"


class TestAutoApproval:
    def test_only_low_reasons_auto_approved(self):
        rd = compute_review_decision(
            _CLEAN,
            supplier_reasons=["deposit_component_detected:1.00", "page_quality_low"],
            confidence=0.9,
        )
        assert rd.auto_approved is True

    def test_medium_reason_not_auto_approved(self):
        rd = compute_review_decision(
            _CLEAN,
            supplier_reasons=["supplier_low_confidence"],
            confidence=0.9,
        )
        assert rd.auto_approved is False


class TestReviewFields:
    def test_fields_listed_for_supplier_issues(self):
        rd = compute_review_decision(
            {**_CLEAN, "supplier_name": None},
            confidence=0.9,
        )
        assert "supplier_name" in rd.review_fields

    def test_fields_listed_for_amount_issues(self):
        rd = compute_review_decision(
            {**_CLEAN, "total_amount": None},
            confidence=0.9,
        )
        assert "total_amount" in rd.review_fields


class TestReturnType:
    def test_returns_review_decision(self):
        rd = compute_review_decision(_CLEAN)
        assert isinstance(rd, ReviewDecision)
        assert hasattr(rd, "review_required")
        assert hasattr(rd, "review_priority")
        assert hasattr(rd, "review_reasons")
        assert hasattr(rd, "auto_approved")
