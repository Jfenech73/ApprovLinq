from __future__ import annotations

import os
import re

from app.services.extractor import _build_rows_from_ai_items, split_line_item_rows


def _load_bcrs_helpers():
    src = open(os.path.join(os.path.dirname(__file__), '..', 'app', 'routers', 'batches.py')).read()
    start = src.find('\ndef _parse_money_candidates')
    end = src.find('\ndef _match_supplier_fuzzy')
    ns = {'re': re}
    exec(src[start:end], ns)
    return ns['_extract_bcrs_amount_from_summary'], ns['_page_has_existing_bcrs_row']


_extract_bcrs_amount_from_summary, _page_has_existing_bcrs_row = _load_bcrs_helpers()


class DummyRow:
    def __init__(self, description=None, line_items_raw=None, total_amount=None, net_amount=None):
        self.description = description
        self.line_items_raw = line_items_raw
        self.total_amount = total_amount
        self.net_amount = net_amount


def test_ai_line_builder_uses_invoice_vat_for_single_line():
    page_result = {
        'description': 'Invoice',
        'net_amount': 100.0,
        'vat_amount': 18.0,
        'total_amount': 118.0,
    }
    rows = _build_rows_from_ai_items(page_result, [
        {'description': 'Service fee', 'net_amount': 100.0, 'tax_amount': None}
    ])
    assert len(rows) == 1
    assert rows[0]['net_amount'] == 100.0
    assert rows[0]['vat_amount'] == 18.0
    assert rows[0]['total_amount'] == 118.0
    assert rows[0]['source_invoice_total_amount'] == 118.0
    assert rows[0].get('review_required') is not True


def test_rule_based_line_split_uses_invoice_vat_for_single_line():
    page_result = {
        'description': 'Invoice',
        'line_items_raw': 'Widget A 100.00',
        'net_amount': 100.0,
        'vat_amount': 18.0,
        'total_amount': 118.0,
    }
    rows = split_line_item_rows(page_result)
    assert len(rows) == 1
    assert rows[0]['net_amount'] == 100.0
    assert rows[0]['vat_amount'] == 18.0
    assert rows[0]['total_amount'] == 118.0
    assert rows[0]['source_invoice_total_amount'] == 118.0
    assert rows[0].get('review_required') is not True


def test_bcrs_detection_uses_source_invoice_totals_for_line_rows():
    payload = {
        'totals_raw': 'Net 76.26\nVAT 13.73\nBCRS Deposit 2.40\nTotal Due 92.39',
        'net_amount': 10.0,
        'vat_amount': 0.0,
        'total_amount': 10.0,
        'source_invoice_net_amount': 76.26,
        'source_invoice_vat_amount': 13.73,
        'source_invoice_total_amount': 92.39,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 2.40


def test_bcrs_detection_accepts_deposits_summary_wording():
    payload = {
        'totals_raw': 'Invoice Summary\nNet 50.00\nVAT 9.00\nDeposits 4.80\nInvoice Total 63.80',
        'net_amount': 50.0,
        'vat_amount': 9.0,
        'total_amount': 63.80,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 4.80


def test_existing_deposit_line_prevents_duplicate_bcrs_row():
    rows = [
        DummyRow(description='Water 24x1L', line_items_raw='Water 24x1L = 20.00', total_amount=20.0, net_amount=20.0),
        DummyRow(description='Deposit returnables', line_items_raw='Deposit returnables = 4.80', total_amount=4.8, net_amount=4.8),
    ]
    assert _page_has_existing_bcrs_row(rows, 4.8) is True
