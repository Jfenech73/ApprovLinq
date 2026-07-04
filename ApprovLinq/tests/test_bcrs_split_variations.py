from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.db.models import InvoiceRow
from app.routers.batches import (
    _apply_bcrs_split,
    _decide_bcrs_split,
    _extract_bcrs_amount_from_summary,
    _parse_money_candidates,
)
from app.services.extractor import _extract_labeled_financial_bundle


class DummyDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def query(self, *args, **kwargs):
        raise RuntimeError("not needed")


def _row(total=120.40):
    return InvoiceRow(
        batch_id=uuid4(),
        tenant_id=uuid4(),
        company_id=uuid4(),
        source_file_id=1,
        source_filename="invoice.pdf",
        page_no=1,
        supplier_name="Supplier Ltd",
        supplier_posting_account="SUP-001",
        nominal_account_code="4000",
        invoice_number="INV-1",
        invoice_date=None,
        description="Invoice goods",
        line_items_raw="Goods",
        net_amount=100.0,
        vat_amount=18.0,
        total_amount=total,
        currency="EUR",
        tax_code="T1",
        method_used="test",
        confidence_score=0.9,
        validation_status="ok",
        review_required=False,
        review_priority=None,
        review_reasons=None,
        review_fields=None,
        auto_approved=False,
        page_quality_score=0.9,
        supplier_match_method="exact",
        totals_reconciliation_status="reconciled_with_bcrs",
        header_raw="Supplier Ltd",
        totals_raw="Net 100.00\nVAT 18.00\nContainer levy 2.40\nTotal 120.40",
        page_text_raw=None,
    )


def test_component_variation_is_detected_from_summary():
    payload = {
        "totals_raw": "Net 100.00\nVAT 18.00\nContainer levy 2.40\nTotal 120.40",
        "net_amount": 100.0,
        "vat_amount": 18.0,
        "total_amount": 120.40,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 2.40


def test_extracted_candidate_can_drive_auto_split():
    db = DummyDb()
    batch = SimpleNamespace(tenant_id=uuid4())
    row = _row()
    payload = {
        "_deposit_candidate": 2.40,
        "totals_raw": "Net 100.00\nVAT 18.00\nEco contribution 2.40\nTotal 120.40",
        "net_amount": 100.0,
        "vat_amount": 18.0,
        "total_amount": 120.40,
    }

    outcome, amount, reason = _decide_bcrs_split(db, batch, row, payload, [row])

    assert outcome == "auto_split"
    assert amount == 2.40
    assert reason is None


def test_apply_split_copies_invoice_details_and_reduces_original_total():
    db = DummyDb()
    row = _row()

    bcrs_row = _apply_bcrs_split(db, row, 2.40)

    assert db.added == [bcrs_row]
    assert row.total_amount == 118.0
    assert bcrs_row.total_amount == 2.40
    assert bcrs_row.net_amount == 2.40
    assert bcrs_row.vat_amount == 0.0
    assert bcrs_row.supplier_name == row.supplier_name
    assert bcrs_row.supplier_posting_account == row.supplier_posting_account
    assert bcrs_row.nominal_account_code == row.nominal_account_code
    assert bcrs_row.invoice_number == row.invoice_number
    assert bcrs_row.currency == row.currency


def test_jsultana_totals_table_keeps_bcrs_separate_from_vat():
    text = (
        "Subtotal:\n486.03\n"
        "VAT:\n87.47\n"
        "Terms and Conditions\n"
        "BCRS Deposit:\n68.40\n"
        "Total :\n641.90\n"
        "Balance :\n641.90\n"
        "Tax summary F=18.00% E=0%\n"
        "573.46 @ 18.00% = 87.47"
    )

    bundle = _extract_labeled_financial_bundle(text)

    assert bundle["net_amount"] == 486.03
    assert bundle["vat_amount"] == 87.47
    assert bundle["total_amount"] == 641.90
    assert bundle["_deposit_candidate"] == 68.40


def test_comma_decimal_bcrs_amount_is_not_treated_as_thousands():
    assert _parse_money_candidates("BCRS Deposit: 14,40") == [14.40]
