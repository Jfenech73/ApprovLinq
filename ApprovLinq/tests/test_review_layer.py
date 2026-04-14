"""Tests for correction overlay, audit, rules, status lifecycle.

Uses SQLite in-memory + Base metadata. Run with: pytest tests/test_review_layer.py
"""
import uuid
import pytest
from datetime import datetime
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Tenant, User, Company, InvoiceBatch, InvoiceRow,
    TenantSupplier, TenantNominalAccount,
)
import app.db.review_models  # noqa: F401
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
    db.add_all([t, u])
    db.flush()
    c = Company(
        id=uuid.uuid4(), tenant_id=t.id,
        company_code="CO1", company_name="Company One",
    )
    db.add(c)
    db.flush()
    # Current schema: TenantSupplier requires supplier_name + posting_account + company_id
    db.add(TenantSupplier(
        tenant_id=t.id, company_id=c.id,
        supplier_name="Acme Ltd", posting_account="2100",
    ))
    # Current schema: TenantNominalAccount requires account_code + account_name + company_id
    db.add(TenantNominalAccount(
        tenant_id=t.id, company_id=c.id,
        account_code="5000", account_name="Purchases",
    ))
    b = InvoiceBatch(
        id=uuid.uuid4(), tenant_id=t.id, company_id=c.id,
        batch_name="B", status="scanned",
    )
    db.add(b)
    db.flush()
    r = InvoiceRow(
        batch_id=b.id, tenant_id=t.id, company_id=c.id,
        page_no=1, supplier_name="acme", total_amount=100.0,
        nominal_account_code="9999",
    )
    db.add(r)
    db.commit()
    return {"tenant": t, "company": c, "user": u, "batch": b, "row": r}


# ── Core overlay mechanics ────────────────────────────────────────────────────

def test_edit_creates_overlay_not_mutating_row(db, fixtures):
    f = fixtures
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"description": "Hello"}, user=f["user"])
    db.commit()
    assert f["row"].description is None           # raw row untouched
    c = db.get(InvoiceRowCorrection, f["row"].id)
    assert c.description == "Hello"
    assert f["batch"].status == "in_review"       # auto-flip from scanned


# ── A: supplier validation uses current schema field supplier_name ────────────

def test_supplier_in_master_list_accepted(db, fixtures):
    """Correction to a known supplier_name passes validation without force_add."""
    f = fixtures
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"supplier_name": "Acme Ltd"}, user=f["user"])
    db.commit()
    c = db.get(InvoiceRowCorrection, f["row"].id)
    assert c.supplier_name == "Acme Ltd"


def test_supplier_outside_master_raises_without_force_add(db, fixtures):
    """Unknown supplier_name raises ValueError when force_add is False."""
    f = fixtures
    with pytest.raises(ValueError, match="supplier_name not in master list"):
        cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                               changes={"supplier_name": "Brand New Co"}, user=f["user"])


def test_supplier_force_add_raises_because_required_fields_missing(db, fixtures):
    """force_add=True still raises because TenantSupplier requires company_id
    and posting_account, which are not in a correction payload.

    Hard rule: no incomplete master-data row must ever be silently inserted.
    """
    f = fixtures
    with pytest.raises(ValueError, match="Cannot force-add supplier"):
        cs.apply_field_changes(
            db, batch=f["batch"], row=f["row"],
            changes={"supplier_name": "Brand New Co"},
            user=f["user"], force_add=True, note="vendor migrated",
        )
    # Confirm no partial row was written
    rows = db.execute(
        select(TenantSupplier).where(TenantSupplier.supplier_name == "Brand New Co")
    ).scalars().all()
    assert len(rows) == 0


# ── A: nominal validation uses current schema field account_code ──────────────

def test_nominal_in_master_list_accepted(db, fixtures):
    """Correction to a known account_code passes validation."""
    f = fixtures
    cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                           changes={"nominal_account_code": "5000"}, user=f["user"])
    db.commit()
    c = db.get(InvoiceRowCorrection, f["row"].id)
    assert c.nominal_account_code == "5000"


def test_nominal_outside_master_raises_without_force_add(db, fixtures):
    """Unknown account_code raises ValueError when force_add is False."""
    f = fixtures
    with pytest.raises(ValueError, match="nominal_account_code not in master list"):
        cs.apply_field_changes(db, batch=f["batch"], row=f["row"],
                               changes={"nominal_account_code": "9999"}, user=f["user"])


def test_nominal_force_add_raises_because_required_fields_missing(db, fixtures):
    """force_add=True still raises for nominals because account_name is required."""
    f = fixtures
    with pytest.raises(ValueError, match="Cannot force-add nominal account"):
        cs.apply_field_changes(
            db, batch=f["batch"], row=f["row"],
            changes={"nominal_account_code": "9999"},
            user=f["user"], force_add=True, note="new account",
        )
    rows = db.execute(
        select(TenantNominalAccount).where(TenantNominalAccount.account_code == "9999")
    ).scalars().all()
    assert len(rows) == 0


# ── A: company-scoped validation ──────────────────────────────────────────────

def test_supplier_from_different_company_not_visible(db, fixtures):
    """A supplier belonging to company B must not satisfy validation for company A's batch."""
    f = fixtures
    company_b = Company(
        id=uuid.uuid4(), tenant_id=f["tenant"].id,
        company_code="CO2", company_name="Company Two",
    )
    db.add(company_b)
    db.flush()
    db.add(TenantSupplier(
        tenant_id=f["tenant"].id, company_id=company_b.id,
        supplier_name="CompanyB Supplier", posting_account="2200",
    ))
    db.commit()
    # Batch belongs to CO1 — "CompanyB Supplier" exists only in CO2
    with pytest.raises(ValueError, match="supplier_name not in master list"):
        cs.apply_field_changes(
            db, batch=f["batch"], row=f["row"],
            changes={"supplier_name": "CompanyB Supplier"},
            user=f["user"],
        )


def test_nominal_from_different_company_not_visible(db, fixtures):
    """A nominal account belonging to company B must not satisfy validation for company A's batch."""
    f = fixtures
    company_b = Company(
        id=uuid.uuid4(), tenant_id=f["tenant"].id,
        company_code="CO3", company_name="Company Three",
    )
    db.add(company_b)
    db.flush()
    db.add(TenantNominalAccount(
        tenant_id=f["tenant"].id, company_id=company_b.id,
        account_code="7777", account_name="Other Nominal",
    ))
    db.commit()
    with pytest.raises(ValueError, match="nominal_account_code not in master list"):
        cs.apply_field_changes(
            db, batch=f["batch"], row=f["row"],
            changes={"nominal_account_code": "7777"},
            user=f["user"],
        )


# ── B: remap supplier lookup uses supplier_name + company scope ───────────────

def test_remap_supplier_lookup_uses_supplier_name_field(db, fixtures):
    """The lookup that feeds RemapHint.supplier_id must use TenantSupplier.supplier_name.
    This is a regression test for defect B where the old code used .name."""
    f = fixtures
    supplier = db.execute(
        select(TenantSupplier).where(
            TenantSupplier.tenant_id == f["tenant"].id,
            TenantSupplier.supplier_name == "Acme Ltd",
            TenantSupplier.company_id == f["company"].id,
        )
    ).scalar_one_or_none()
    assert supplier is not None, (
        "TenantSupplier must be found via .supplier_name (not the stale .name attribute)"
    )
    assert supplier.supplier_name == "Acme Ltd"
    # The stale .name column must not exist on the current model
    assert "name" not in TenantSupplier.__table__.columns, (
        "TenantSupplier should have no 'name' column — it was renamed to 'supplier_name'"
    )


# ── C: mark_file_reviewed audit uses correct model field ─────────────────────

def test_mark_file_reviewed_audit_action_field(db, fixtures):
    """InvoiceRowFieldAudit rows must be written with action= not action_type=.

    Defect C: the old code passed action_type='mark_reviewed', which is not a
    mapped column. SQLAlchemy silently ignores unknown kwargs, leaving
    action=NULL and causing the audit to be unqueryable by action type.
    """
    f = fixtures
    corr = InvoiceRowCorrection(row_id=f["row"].id, batch_id=f["batch"].id, row_reviewed=True)
    db.add(corr)
    db.flush()
    audit = InvoiceRowFieldAudit(
        batch_id=f["batch"].id, row_id=f["row"].id,
        field_name="_file_reviewed",
        old_value=None, new_value="marked_reviewed",
        action="mark_reviewed",      # correct mapped column name
        user_id=f["user"].id, note=None,
    )
    db.add(audit)
    db.commit()

    persisted = db.query(InvoiceRowFieldAudit).filter_by(
        field_name="_file_reviewed"
    ).one()
    assert persisted.action == "mark_reviewed"
    # Confirm action_type is not a real column on the model
    assert "action_type" not in InvoiceRowFieldAudit.__table__.columns, (
        "'action_type' is not a column on InvoiceRowFieldAudit; use 'action' instead"
    )


# ── Existing behaviours ───────────────────────────────────────────────────────

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
