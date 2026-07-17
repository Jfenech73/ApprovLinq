from __future__ import annotations

import os
import re

from app.services import extractor
from app.services.extractor import _build_direct_di_page_rows, _build_rows_from_ai_items, process_pdf_page_rows, split_line_item_rows


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


def test_ai_line_builder_allocates_invoice_vat_across_multiple_net_lines():
    page_result = {
        'description': 'Invoice',
        'supplier_name': 'Supplier Ltd',
        'invoice_number': 'INV-LINES',
        'invoice_date': '2026-07-17',
        'net_amount': 100.0,
        'vat_amount': 18.0,
        'total_amount': 118.0,
        'currency': 'EUR',
    }
    rows = _build_rows_from_ai_items(page_result, [
        {'description': 'Widget A', 'net_amount': 40.0, 'tax_amount': None},
        {'description': 'Widget B', 'net_amount': 60.0, 'tax_amount': None},
    ])
    assert [row['description'] for row in rows] == ['Widget A', 'Widget B']
    assert [row['supplier_name'] for row in rows] == ['Supplier Ltd', 'Supplier Ltd']
    assert [row['invoice_number'] for row in rows] == ['INV-LINES', 'INV-LINES']
    assert [row['vat_amount'] for row in rows] == [7.2, 10.8]
    assert round(sum(row['total_amount'] for row in rows), 2) == 118.0
    assert all(row.get('review_required') is not True for row in rows)


def test_ai_line_builder_handles_gross_line_amounts_without_double_vat():
    page_result = {
        'description': 'Invoice',
        'net_amount': 100.0,
        'vat_amount': 18.0,
        'total_amount': 118.0,
    }
    rows = _build_rows_from_ai_items(page_result, [
        {'description': 'Gross line A', 'amount': 59.0},
        {'description': 'Gross line B', 'amount': 59.0},
    ])
    assert [row['net_amount'] for row in rows] == [50.0, 50.0]
    assert [row['vat_amount'] for row in rows] == [9.0, 9.0]
    assert [row['total_amount'] for row in rows] == [59.0, 59.0]
    assert all(row.get('review_required') is not True for row in rows)


def test_line_builder_preserves_bcrs_as_non_vat_line_and_reconciles():
    page_result = {
        'description': 'Invoice',
        'net_amount': 76.26,
        'vat_amount': 13.73,
        'total_amount': 92.39,
    }
    rows = _build_rows_from_ai_items(page_result, [
        {'description': 'Water 24x1L', 'net_amount': 76.26, 'tax_amount': None},
        {'description': 'BCRS refundable deposit', 'net_amount': 2.40, 'tax_amount': None},
    ])
    assert len(rows) == 2
    assert rows[0]['vat_amount'] == 13.73
    assert rows[0]['total_amount'] == 89.99
    assert rows[1]['net_amount'] == 2.4
    assert rows[1]['vat_amount'] == 0.0
    assert rows[1]['total_amount'] == 2.4
    assert round(sum(row['total_amount'] for row in rows), 2) == 92.39
    assert all(row.get('review_required') is not True for row in rows)


def test_rule_based_line_split_filters_summary_rows_and_allocates_vat():
    page_result = {
        'description': 'Invoice',
        'line_items_raw': 'Widget A 40.00\nWidget B 60.00\nVAT 18.00\nInvoice Total 118.00',
        'net_amount': 100.0,
        'vat_amount': 18.0,
        'total_amount': 118.0,
    }
    rows = split_line_item_rows(page_result)
    assert [row['description'] for row in rows] == ['Widget A 40.00', 'Widget B 60.00']
    assert [row['vat_amount'] for row in rows] == [7.2, 10.8]
    assert round(sum(row['total_amount'] for row in rows), 2) == 118.0


def _di_text(value):
    return {'content': str(value), 'value_string': str(value)}


def _di_money(value):
    return {'content': f'{float(value):.2f}', 'value_currency': {'amount': float(value), 'currency_code': 'EUR'}}


def _di_number(value):
    return {'content': str(value), 'value_number': float(value)}


def _di_item(description, amount, tax=None, quantity=None, unit_price=None):
    fields = {
        'Description': _di_text(description),
        'Amount': _di_money(amount),
    }
    if tax is not None:
        fields['Tax'] = _di_money(tax)
    if quantity is not None:
        fields['Quantity'] = _di_number(quantity)
    if unit_price is not None:
        fields['UnitPrice'] = _di_money(unit_price)
    return {'value_object': fields}


def _direct_di_payload(*, subtotal, tax, total, items, total_discount=None, content='Invoice content'):
    fields = {
        'VendorName': _di_text('Supplier Ltd'),
        'VendorTaxId': _di_text('MT12345678'),
        'InvoiceId': _di_text('INV-DI-1'),
        'InvoiceDate': _di_text('17/07/2026'),
        'SubTotal': _di_money(subtotal),
        'TotalTax': _di_money(tax),
        'InvoiceTotal': _di_money(total),
        'CurrencyCode': _di_text('EUR'),
        'Items': {'value_array': list(items)},
    }
    if total_discount is not None:
        fields['TotalDiscount'] = _di_money(total_discount)
    return {
        '_di_raw_fields': fields,
        '_di_raw_payload': {
            'content': content,
            'document': {'confidence': 0.94, 'doc_type': 'invoice'},
        },
    }


def _patch_direct_di(monkeypatch, payload):
    monkeypatch.setattr(extractor, 'azure_di_available', lambda: (True, None))
    monkeypatch.setattr(extractor.OCRBackend, 'render_pdf_page_to_jpeg_bytes', lambda *a, **k: b'jpg')
    monkeypatch.setattr(extractor, 'azure_di_extract_invoice', lambda *a, **k: payload)
    monkeypatch.setattr(extractor, 'extract_native_pdf_page', lambda *a, **k: None)
    monkeypatch.setattr(extractor, '_apply_direct_di_content_blank_fallbacks', lambda *a, **k: None)
    monkeypatch.setattr(extractor, '_apply_direct_blank_field_fallbacks', lambda *a, **k: None)


def test_direct_di_summary_mode_still_returns_single_invoice_row(monkeypatch, tmp_path):
    payload = _direct_di_payload(
        subtotal=100.0,
        tax=18.0,
        total=118.0,
        items=[
            _di_item('Widget A', 40.0, tax=7.2),
            _di_item('Widget B', 60.0, tax=10.8),
        ],
    )
    _patch_direct_di(monkeypatch, payload)

    rows = _build_direct_di_page_rows(tmp_path / 'invoice.pdf', 0, scan_mode='summary')

    assert len(rows) == 1
    assert rows[0]['description'] == 'Widget A; Widget B'
    assert rows[0]['net_amount'] == 100.0
    assert rows[0]['vat_amount'] == 18.0
    assert rows[0]['total_amount'] == 118.0


def test_direct_di_lines_mode_outputs_one_row_per_item_with_discount(monkeypatch, tmp_path):
    payload = _direct_di_payload(
        subtotal=90.0,
        tax=16.2,
        total=106.2,
        total_discount=10.0,
        items=[
            _di_item('Widget A', 60.0, quantity=3, unit_price=20.0),
            _di_item('Widget B', 40.0, quantity=2, unit_price=20.0),
        ],
    )
    _patch_direct_di(monkeypatch, payload)

    rows = _build_direct_di_page_rows(tmp_path / 'invoice.pdf', 0, scan_mode='lines')

    assert [row['description'] for row in rows] == ['Widget A', 'Widget B']
    assert [row['supplier_name'] for row in rows] == ['Supplier Ltd', 'Supplier Ltd']
    assert [row['invoice_number'] for row in rows] == ['INV-DI-1', 'INV-DI-1']
    assert [row['net_amount'] for row in rows] == [54.0, 36.0]
    assert [row['vat_amount'] for row in rows] == [9.72, 6.48]
    assert round(sum(row['total_amount'] for row in rows), 2) == 106.2
    assert all('lines' in row['method_used'] for row in rows)


def test_direct_di_lines_mode_aggregates_bcrs_as_one_extra_row(monkeypatch, tmp_path):
    payload = _direct_di_payload(
        subtotal=76.26,
        tax=13.73,
        total=92.39,
        items=[
            _di_item('Water 24x1L', 76.26),
            _di_item('BCRS refundable deposit', 1.20),
            _di_item('BCRS refundable deposit', 1.20),
        ],
    )
    _patch_direct_di(monkeypatch, payload)

    rows = _build_direct_di_page_rows(tmp_path / 'invoice.pdf', 0, scan_mode='lines')

    assert [row['description'] for row in rows] == ['Water 24x1L', 'BCRS refundable deposit']
    assert rows[0]['net_amount'] == 76.26
    assert rows[0]['vat_amount'] == 13.73
    assert rows[0]['total_amount'] == 89.99
    assert rows[1]['net_amount'] == 2.4
    assert rows[1]['vat_amount'] == 0.0
    assert rows[1]['total_amount'] == 2.4
    assert round(sum(row['total_amount'] for row in rows), 2) == 92.39


def test_process_pdf_page_rows_passes_lines_mode_to_direct_di(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(extractor.settings, 'scan_provider_baseline_mode', True)
    monkeypatch.setattr(extractor.settings, 'use_azure_di', True)

    def fake_direct(pdf_path, page_index, scan_mode='summary', openai_api_key=None, account_company_name=None):
        calls.append(scan_mode)
        return [{'description': 'Line item', 'line_no': 1}]

    monkeypatch.setattr(extractor, '_build_direct_di_page_rows', fake_direct)

    rows = process_pdf_page_rows(tmp_path / 'invoice.pdf', 0, scan_mode='lines')

    assert calls == ['lines']
    assert rows == [{'description': 'Line item', 'line_no': 1}]


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
