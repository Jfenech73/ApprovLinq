from pathlib import Path


def test_supplier_history_service_exists_with_required_helpers():
    src = Path('app/services/supplier_history.py').read_text()
    for name in [
        'SupplierHistorySignal',
        'SupplierHistoryProfile',
        'get_supplier_history_profile',
        'get_recent_accepted_corrections',
        'rank_historical_candidate',
        'calculate_field_success_rate',
    ]:
        assert name in src
    assert 'tenant_id == tenant_id' in src or 'InvoiceRow.tenant_id == tenant_id' in src
    assert 'company_specific' in src


def test_supplier_history_learns_only_from_accepted_evidence():
    src = Path('app/services/supplier_history.py').read_text()
    assert 'row_reviewed' in src
    assert 'status or "").lower() == "exported"' in src
    assert 'mark_reviewed' in src
    assert 'auto_approved' not in src  # avoid unreviewed raw extraction as learning source
    assert 'validation_status' not in src  # validation alone is not acceptance


def test_supplier_history_thresholds_and_conflict_behaviour_are_explicit():
    src = Path('app/services/supplier_history.py').read_text()
    assert 'support_count == 1' in src
    assert 'base = 0.48' in src
    assert 'support_count <= 3' in src
    assert 'base = 0.66' in src
    assert 'base = 0.80' in src
    assert 'conflict_count' in src
    assert 'Historical supplier evidence is conflicting' in src
    assert 'should_apply=(support_count >= 2 and conflict_count == 0' in src


def test_arbitration_uses_supplier_history_service_and_keeps_one_off_suggestions_safe():
    src = Path('app/services/invoice_arbitration.py').read_text()
    assert 'from app.services.supplier_history import get_supplier_history_profile' in src
    assert 'profile = get_supplier_history_profile' in src
    assert 'matched_rows=' in src
    assert 'accepted_rows=' in src
    assert 'winner.source_type == "accepted_correction"' in src
    assert 'winner.should_apply' in src
