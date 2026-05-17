from __future__ import annotations

import os
import re
from types import SimpleNamespace

import app.services.extractor as extractor


def _batch_src() -> str:
    return open(os.path.join(os.path.dirname(__file__), '..', 'app', 'routers', 'batches.py')).read()


def _load_batch_helpers():
    src = _batch_src()
    start = src.find('def _normalise_text_signature')
    end = src.find('def _match_supplier_fuzzy')
    ns = {'re': re, 'logger': SimpleNamespace(debug=lambda *a, **k: None), 'Session': object, 'InvoiceBatch': object, 'InvoiceRow': object, 'RemapHint': object}
    exec(src[start:end], ns)
    return ns


B = _load_batch_helpers()


class _FakeQuery:
    def __init__(self, items):
        self._items = items
    def filter(self, *args, **kwargs):
        return self
    def all(self):
        return list(self._items)
    def count(self):
        return len(self._items)


class _FakeDB:
    def __init__(self, hints=None):
        self._hints = hints or []
    def query(self, model):
        return _FakeQuery(self._hints)


def test_decide_bcrs_review_suggest_for_unresolved_deposit_signal():
    batch = SimpleNamespace(tenant_id='t1')
    row = SimpleNamespace(supplier_name='ABC Ltd')
    payload = {
        'totals_raw': 'Invoice Summary\nNet 50.00\nVAT 9.00\nDeposit note\nInvoice Total 63.80',
        'page_text_raw': 'Deposit may apply',
        'net_amount': 50.0,
        'vat_amount': 9.0,
        'total_amount': 63.8,
    }
    outcome, amount, reason = B['_decide_bcrs_split'](_FakeDB(), batch, row, payload, [])
    assert outcome == 'review_suggest_split'
    assert amount is None
    assert 'Possible deposit/BCRS' in reason


def test_decide_bcrs_no_duplicate_when_line_already_exists():
    batch = SimpleNamespace(tenant_id='t1')
    row = SimpleNamespace(supplier_name='ABC Ltd')
    payload = {
        'totals_raw': 'Invoice Summary\nNet 50.00\nVAT 9.00\nDeposits 4.80\nInvoice Total 63.80',
        'net_amount': 50.0,
        'vat_amount': 9.0,
        'total_amount': 63.8,
    }
    existing = [SimpleNamespace(description='Deposit returnables', line_items_raw='Deposit returnables = 4.80', total_amount=4.8, net_amount=4.8)]
    outcome, amount, _ = B['_decide_bcrs_split'](_FakeDB(), batch, row, payload, existing)
    assert outcome == 'no_split'
    assert amount is None


def test_signature_fallback_logic_present_for_remap_hints():
    src = _batch_src()
    fn = src[src.find('def _apply_remap_hints'):src.find('def _is_suspect_field_value')]
    assert 'row_signature' in fn
    assert '_signature_overlap' in fn
    assert 'source_row_id' in fn
    assert '>= 0.35' in fn


def test_rank_candidates_with_llm_only_chooses_provided_candidate(monkeypatch):
    monkeypatch.setattr(extractor, '_call_openai', lambda *a, **k: '{"chosen_candidate":"INV-200","confidence":"high","review_recommended":false,"reason":"best label match"}')
    ranked = extractor._rank_candidates_with_llm('invoice_number', ['INV-100', 'INV-200'], 'Invoice INV-200', 'key')
    assert ranked['chosen_candidate'] == 'INV-200'


def test_simple_extract_marks_llm_ranking_source(monkeypatch):
    monkeypatch.setattr(extractor, '_collect_supplier_candidates', lambda *a, **k: ['Wrong Name Ltd', 'Acme Trading Ltd'])
    monkeypatch.setattr(extractor, '_collect_invoice_number_candidates', lambda *a, **k: ['12345', 'INV-12345'])
    monkeypatch.setattr(extractor, '_rank_candidates_with_llm', lambda field_name, candidates, page_text, api_key, model='': {
        'chosen_candidate': candidates[-1], 'confidence': 'high', 'review_recommended': False, 'reason': 'ranked'
    })
    monkeypatch.setattr(extractor, 'summarise_line_items_with_openai', lambda *a, **k: None)
    result = extractor.simple_extract('Acme Trading Ltd\nInvoice Number INV-12345\nTotal 11.80', openai_api_key='key')
    assert result['supplier_name'] == 'Acme Trading Ltd'
    assert result['invoice_number'] == 'INV-12345'
    assert result['_field_sources']['supplier_name'] == 'llm_ranking'
    assert result['_field_sources']['invoice_number'] == 'llm_ranking'
