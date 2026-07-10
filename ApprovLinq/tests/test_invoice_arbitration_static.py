from pathlib import Path


def test_invoice_arbitration_service_exists_with_required_contract():
    src = Path('app/services/invoice_arbitration.py').read_text()
    assert 'class Candidate' in src
    assert 'class ArbitrationResult' in src
    assert 'def arbitrate_invoice_row' in src
    for field in [
        'supplier_name', 'invoice_number', 'invoice_date', 'net_amount',
        'vat_amount', 'total_amount', 'tax_code', 'nominal_account_code',
        'description', 'currency'
    ]:
        assert field in src
    for source in [
        'raw_extraction', 'correction_rule', 'saved_region',
        'supplier_history', 'accepted_correction', 'totals_reconciliation',
        'admin_global_rule'
    ]:
        assert source in src


def test_batch_pipeline_calls_arbitration_after_rules_and_before_bcrs():
    src = Path('app/routers/batches.py').read_text()
    assert 'from app.services.invoice_arbitration import arbitrate_invoice_row' in src
    apply_idx = src.index('_apply_saved_rules(db, batch, row)')
    arb_idx = src.index('arbitrate_invoice_row(db, batch, row, r')
    bcrs_idx = src.index('_decide_bcrs_split(db, batch, row, r, [row])')
    assert apply_idx < arb_idx < bcrs_idx


def test_arbitration_writes_review_and_audit_markers():
    src = Path('app/services/invoice_arbitration.py').read_text()
    assert 'InvoiceRowFieldAudit' in src
    assert 'arbitration_apply' in src
    assert 'arbitration_conflict' in src
    assert 'arbitration_suggest' in src
    assert 'row.review_required = True' in src
    assert 'row.review_fields' in src
    assert 'row.review_reasons' in src
    assert 'arbitrated:' in src


def test_arbitration_does_not_audit_noop_conflict_when_winner_confirms_current_value():
    src = Path('app/services/invoice_arbitration.py').read_text()
    assert '``total_amount arbitration_conflict 9.44 -> 9.44``' in src
    same_idx = src.index('if same:\n            decision.conflict = False')
    conflict_idx = src.index('if conflict:', same_idx)
    assert same_idx < conflict_idx
    assert 'db, batch, row, field_name, current, winner.value,' in src


def test_method_used_is_text_not_varchar_200_to_avoid_koyeb_runtime_truncation():
    model_src = Path('app/db/models.py').read_text()
    migration_src = Path('alembic/versions/2026_07_10_0009_database_migration_foundation.py').read_text()
    batch_src = Path('app/routers/batches.py').read_text()
    assert 'method_used: Mapped[str | None] = mapped_column(Text, nullable=True)' in model_src
    assert '_alter_column_type_if_present("invoice_rows", "method_used", sa.Text())' in migration_src
    assert 'row.method_used = "+".join(parts)[:255]' not in batch_src
