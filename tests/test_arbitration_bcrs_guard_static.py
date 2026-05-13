from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_supplier_history_does_not_arbitrate_invoice_specific_values():
    text = (ROOT / 'app/services/invoice_arbitration.py').read_text()
    assert 'VOLATILE_HISTORY_VALUE_FIELDS' in text
    assert 'invoice_number' in text
    assert 'invoice_date' in text
    assert 'net_amount' in text
    assert 'vat_amount' in text
    assert 'total_amount' in text
    assert 'description' in text
    assert 'signal.field_name in VOLATILE_HISTORY_VALUE_FIELDS' in text
    assert 'continue' in text


def test_bcrs_arithmetic_fallback_requires_explicit_bcrs_label():
    text = (ROOT / 'app/routers/batches.py').read_text()
    assert 'Safe arithmetic fallback' in text
    assert 'explicit_bcrs_label' in text
    assert 'bcrs' in text.lower()
    assert 'refundable\\s+deposit' in text
    assert '_add_candidate(34, diff)' in text
    assert 'diff < float(total_amount)' in text
