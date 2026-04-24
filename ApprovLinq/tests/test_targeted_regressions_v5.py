from __future__ import annotations

import os
from types import SimpleNamespace

import app.services.extractor as extractor


def _batch_src() -> str:
    return open(os.path.join(os.path.dirname(__file__), '..', 'app', 'routers', 'batches.py')).read()


def _load_batch_helpers():
    src = _batch_src()
    start = src.find('def _normalise_text_signature')
    end = src.find('def _apply_remap_hints')
    ns = {'re': __import__('re')}
    exec(src[start:end], ns)
    return ns


B = _load_batch_helpers()


def test_merge_ai_fields_keeps_reconciled_base_vat_when_deposit_explains_total():
    base = {
        'supplier_name': 'Maverick Trading',
        'net_amount': 54.73,
        'vat_amount': 9.85,
        'total_amount': 69.98,
        '_deposit_candidate': 5.40,
    }
    ai = {
        'supplier_name': 'Maverick Trading',
        'net_amount': 54.73,
        'vat_amount': 0.0,
        'total_amount': 69.98,
        'extraction_source': 'azure_di',
        'ai_confidence': {'supplier': 0.95},
    }
    merged = extractor.merge_ai_fields(base, ai)
    assert merged['vat_amount'] == 9.85
    assert merged['net_amount'] == 54.73
    assert merged['total_amount'] == 69.98


def test_supplier_hint_signature_match_uses_snapshot_tokens_when_supplier_blank():
    row = SimpleNamespace(
        header_raw='Mafimex Ltd. The Fruit and Vegetable Centre',
        totals_raw='Total Net 60.06 Total VAT 0.00 Total Gross 60.06',
        page_text_raw='Invoice Delivery Address Pinto Confectionery Mafimex Ltd.',
    )
    hint = SimpleNamespace(field_name='supplier_name', supplier_name_snapshot='Mafimex Ltd.')
    assert B['_supplier_hint_signature_match'](row, hint) is True


def test_supplier_hint_signature_match_rejects_unrelated_supplier():
    row = SimpleNamespace(
        header_raw='BR Supply Co. Cash Sale',
        totals_raw='Total Excl. Vat 33.64',
        page_text_raw='Branram Ltd Malta',
    )
    hint = SimpleNamespace(field_name='supplier_name', supplier_name_snapshot='Mafimex Ltd.')
    assert B['_supplier_hint_signature_match'](row, hint) is False
