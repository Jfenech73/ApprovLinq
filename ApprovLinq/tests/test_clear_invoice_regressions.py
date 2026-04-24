from __future__ import annotations

import os
import re
from types import SimpleNamespace

import app.services.extractor as extractor


def _batch_src() -> str:
    return open(os.path.join(os.path.dirname(__file__), '..', 'app', 'routers', 'batches.py')).read()


def _load_batch_helpers():
    src = _batch_src()
    start = src.find('def _parse_money_candidates')
    end = src.find('def _build_bcrs_row')
    ns = {'re': re, 'logger': SimpleNamespace(debug=lambda *a, **k: None)}
    exec(src[start:end], ns)
    return ns


B = _load_batch_helpers()


def test_merge_ai_fields_prefers_reconciled_base_amounts_when_ai_truncates_digits():
    base = {
        'supplier_name': 'Mafimex Ltd.',
        'net_amount': 2917.67,
        'vat_amount': 525.18,
        'total_amount': 3442.85,
    }
    ai = {
        'supplier_name': 'Mafimex Ltd.',
        'net_amount': 917.67,
        'vat_amount': 525.18,
        'total_amount': 1442.85,
        'extraction_source': 'azure_di',
        'ai_confidence': {'supplier': 0.95},
    }
    merged = extractor.merge_ai_fields(base, ai)
    assert merged['net_amount'] == 2917.67
    assert merged['total_amount'] == 3442.85


def test_merge_ai_fields_keeps_stronger_rule_supplier_when_ai_is_clipped():
    base = {
        'supplier_name': 'Mafimex Ltd.',
        'net_amount': 10.0,
        'vat_amount': 1.8,
        'total_amount': 11.8,
    }
    ai = {
        'supplier_name': 'Mafimex',
        'extraction_source': 'azure_di',
        'ai_confidence': {'supplier': 0.35},
    }
    merged = extractor.merge_ai_fields(base, ai)
    assert merged['supplier_name'] == 'Mafimex Ltd.'


def test_extract_bcrs_amount_handles_parenthetical_refundable_deposit_label():
    payload = {
        'totals_raw': 'Total Net Amount € 54.73\nVAT Amount € 9.85\n* BCRS Refundable Deposit (M) € 5.40\nInvoice Total € 69.98',
        'page_text_raw': '',
        'net_amount': 54.73,
        'vat_amount': 9.85,
        'total_amount': 69.98,
    }
    amount = B['_extract_bcrs_amount_from_summary'](payload)
    assert amount == 5.40
