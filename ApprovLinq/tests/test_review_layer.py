"""Tests for correction overlay, audit, rules, status lifecycle.

These use SQLite in-memory + the existing Base metadata so no real Postgres
is required. Run with: pytest tests/test_review_layer.py
"""
import uuid
import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Tenant, User, InvoiceBatch, InvoiceRow, TenantSupplier, TenantNominalAccount
import app.db.review_models  # noqa: F401  ensures new tables join Base.metadata
from app.db.review_models import InvoiceRowCorrection, InvoiceRowFieldAudit, CorrectionRule
from app.services import correction_service as cs


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session()


@pytest.fixture()
def fixtures(db):
    t = Tenant(id=uuid.uuid4(), tenant_code="t1", tenant_name="T1")
    u = User(id=uuid.uuid4(), email="u@x", full_name="U", password_hash="x", role="admin")
    db.add_all([t, u]); db.flush()
    db.add(TenantSupplier(tenant_id=t.id, name="Acme Ltd"))
    db.add(TenantNominalAccount(tenant_id=t.id, code="5000"))
    b = InvoiceBatch(id=uuid.uuid4(), tenant_id=t.id, batch_name="B", status="scanned")
    db.add(b); db.flush()
    r = InvoiceRow(batch_id=b.id, tenant_id=t.id, page_no=1,
                   supplier_name="acme", total_amount=100.0, nominal_account_code="9999")
    db.add(r); db.commit()
    return {"tenant": t, "user": u, "batch": b, "row": r}


def test_edit_creates_overlay_not_mutating_row(db, fixtures):
    f = fixtures
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"description": "Hello"}, user=f["user"])
    db.commit()
    assert f["row"].description is None  # raw row untouched
    c = db.get(InvoiceRowCorrection, f["row"].id)
    assert c.description == "Hello"
    assert f["batch"].status == "in_review"  # auto-flip from scanned


def test_supplier_outside_master_requires_force_add(db, fixtures):
    f = fixtures
    with pytest.raises(ValueError):
        cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                               changes={"supplier_name": "Brand New Co"}, user=f["user"])
    # With force_add + note it works and adds to master
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"supplier_name": "Brand New Co"},
                           user=f["user"], force_add=True, note="vendor migrated")
    db.commit()
    from sqlalchemy import select
    n = db.execute(select(TenantSupplier).where(TenantSupplier.name == "Brand New Co")).scalar_one()
    assert n is not None
    audit = db.query(InvoiceRowFieldAudit).filter_by(field_name="supplier_name").one()
    assert audit.force_added is True and audit.note == "vendor migrated"


def test_save_as_rule_creates_correction_rule(db, fixtures):
    f = fixtures
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"supplier_name": "Acme Ltd"},
                           user=f["user"], save_as_rule_fields={"supplier_name"})
    db.commit()
    rule = db.query(CorrectionRule).one()
    assert rule.rule_type == "supplier_alias"
    assert rule.source_pattern == "acme"
    assert rule.target_value == "Acme Ltd"


def test_revert_clears_overlay(db, fixtures):
    f = fixtures
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"description": "X"}, user=f["user"])
    db.commit()
    cs.revert_field(db, batch=f["batch"], row=f["row"], field="description", user=f["user"])
    db.commit()
    c = db.get(InvoiceRowCorrection, f["row"].id)
    assert c.description is None
    actions = [a.action for a in db.query(InvoiceRowFieldAudit).all()]
    assert "edit" in actions and "revert" in actions


def test_status_transitions(db, fixtures):
    f = fixtures; b = f["batch"]; u = f["user"]
    cs.transition_status(db, batch=b, target="in_review", user=u)
    cs.transition_status(db, batch=b, target="approved", user=u)
    with pytest.raises(ValueError):
        cs.transition_status(db, batch=b, target="scanned", user=u)
    b.status = "exported"
    with pytest.raises(ValueError):
        cs.transition_status(db, batch=b, target="in_review", user=u)
    cs.reopen_batch(db, batch=b, user=u)
    assert b.status == "in_review"


def test_export_blocks_edits_until_reopen(db, fixtures):
    f = fixtures; f["batch"].status = "exported"
    with pytest.raises(PermissionError):
        cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                               changes={"description": "nope"}, user=f["user"])
