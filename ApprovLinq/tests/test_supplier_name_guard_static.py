from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_saved_region_supplier_identity_guard_exists():
    text = (ROOT / 'app/routers/batches.py').read_text()
    assert 'Supplier-name regions are allowed to *confirm or complete* the same supplier' in text
    assert 'wrong-supplier changes' in text
    assert 'Saved supplier region did not match the existing supplier relationship' in text
    assert 'Skipped saved-region supplier overwrite because the region did not match the existing supplier relationship' in text


def test_arbitration_supplier_identity_guard_exists():
    text = (ROOT / 'app/services/invoice_arbitration.py').read_text()
    assert 'Supplier name is identity data' in text
    assert 'explicit tenant correction rules may rename a strong supplier value' in text
    assert "winner.source_type not in EXPLICIT_RULE_SOURCES" in text
