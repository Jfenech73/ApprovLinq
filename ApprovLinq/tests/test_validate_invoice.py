"""Tests for app.services.validate_invoice."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.validate_invoice import validate_invoice


class TestCleanReconciliation:
    def test_exact_match(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 23.0, "total_amount": 123.0})
        assert vi.totals_reconciliation_status == "ok"
        assert vi.review_reasons == []

    def test_within_tolerance(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 23.0, "total_amount": 123.05})
        assert vi.totals_reconciliation_status == "ok"

    def test_no_vat_exact_match(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 0.0, "total_amount": 100.0})
        assert vi.totals_reconciliation_status == "ok"


class TestDepositDetection:
    def test_deposit_one_euro(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 23.0, "total_amount": 124.0})
        assert vi.totals_reconciliation_status == "ok_with_deposit"
        assert vi.deposit_amount == 1.0
        assert any("deposit_component_detected" in r for r in vi.review_reasons)

    def test_deposit_twenty_cent(self):
        vi = validate_invoice({"net_amount": 200.0, "vat_amount": 46.0, "total_amount": 246.20})
        assert vi.totals_reconciliation_status == "ok_with_deposit"
        assert abs((vi.deposit_amount or 0) - 0.20) < 0.01

    def test_large_mismatch_not_deposit(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 23.0, "total_amount": 200.0})
        assert vi.totals_reconciliation_status == "totals_mismatch"
        assert "totals_mismatch" in vi.review_reasons


class TestDiscountDetection:
    def test_small_discount(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 23.0, "total_amount": 120.00})
        assert vi.totals_reconciliation_status in ("ok_with_discount", "totals_mismatch")

    def test_negative_diff_large(self):
        vi = validate_invoice({"net_amount": 200.0, "vat_amount": 46.0, "total_amount": 190.0})
        assert vi.totals_reconciliation_status in ("ok_with_discount", "totals_mismatch")


class TestVATChecks:
    def test_normal_vat_rate(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 23.0, "total_amount": 123.0})
        assert vi.vat_rate is not None
        assert abs(vi.vat_rate - 0.23) < 0.001
        assert "vat_anomaly" not in vi.review_reasons

    def test_zero_vat_allowed(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 0.0, "total_amount": 100.0})
        assert "vat_anomaly" not in vi.review_reasons

    def test_vat_too_high(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": 50.0, "total_amount": 150.0})
        assert "vat_anomaly" in vi.review_reasons

    def test_vat_missing_when_total_exceeds_net(self):
        vi = validate_invoice({"net_amount": 100.0, "vat_amount": None, "total_amount": 130.0})
        assert "vat_missing" in vi.review_reasons


class TestMissingAmounts:
    def test_both_missing(self):
        vi = validate_invoice({})
        assert vi.totals_reconciliation_status == "subtotal_not_found"
        assert "subtotal_not_found" in vi.review_reasons

    def test_only_net_missing(self):
        vi = validate_invoice({"vat_amount": 23.0, "total_amount": 123.0})
        assert vi.net_amount is None

    def test_string_amounts_coerced(self):
        vi = validate_invoice({"net_amount": "100.00", "vat_amount": "23.00", "total_amount": "123.00"})
        assert vi.totals_reconciliation_status == "ok"
