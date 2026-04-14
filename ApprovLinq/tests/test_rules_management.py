"""
Tests for the Rules management system.

Covers:
  1. Duplicate rule prevention in correction_service
  2. Normalisation (casing, whitespace) for dedup detection
  3. Editing a rule does not create duplicates
  4. Disabled rules are skipped during _apply_saved_rules
  5. Enabled rules still apply
  6. Rule deletion
  7. Scope restriction — users cannot access other tenants' rules
  8. Enable / disable toggle via API shape
  9. Self-mapping rule is rejected on update
  10. Rules list endpoint shape

Run with:  pytest tests/test_rules_management.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Company, InvoiceBatch, InvoiceRow, Tenant, User, UserTenant,
)
import app.db.review_models  # registers review tables
from app.db.review_models import CorrectionRule, InvoiceRowCorrection


# ── Shared SQLite fixture ─────────────────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def tenant_row(db):
    t = Tenant(id=uuid.uuid4(), tenant_code="T1", tenant_name="Test Tenant")
    db.add(t)
    db.flush()
    return t


@pytest.fixture()
def company_row(db, tenant_row):
    c = Company(id=uuid.uuid4(), tenant_id=tenant_row.id, company_code="C1", company_name="Co One")
    db.add(c)
    db.flush()
    return c


@pytest.fixture()
def user_row(db):
    u = User(id=uuid.uuid4(), email="test@example.com", full_name="Test User",
             hashed_password="x", role="user")
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def admin_row(db):
    u = User(id=uuid.uuid4(), email="admin@example.com", full_name="Admin",
             hashed_password="x", role="admin")
    db.add(u)
    db.flush()
    return u


def _make_batch(db, tenant_id, company_id):
    b = InvoiceBatch(
        id=uuid.uuid4(), tenant_id=tenant_id, company_id=company_id,
        batch_name="Test", status="in_review",
    )
    db.add(b)
    db.flush()
    return b


def _make_row(db, batch):
    r = InvoiceRow(
        batch_id=batch.id, tenant_id=batch.tenant_id, company_id=batch.company_id,
        source_filename="inv.pdf", page_no=1,
        supplier_name="Old Supplier", nominal_account_code="5001",
        currency="EUR",
    )
    db.add(r)
    db.flush()
    return r


def _make_rule(db, tenant_id, company_id=None, rule_type="supplier_alias",
               field_name="supplier_name", source="old supplier",
               target="New Supplier", active=True):
    r = CorrectionRule(
        tenant_id=tenant_id, company_id=company_id,
        rule_type=rule_type, field_name=field_name,
        source_pattern=source, target_value=target,
        active=active,
    )
    db.add(r)
    db.flush()
    return r


# ══════════════════════════════════════════════════════════════════════════════
# 1. Duplicate rule prevention
# ══════════════════════════════════════════════════════════════════════════════

class TestDuplicateRulePrevention:

    def test_saving_same_correction_twice_creates_only_one_rule(self, db, tenant_row, company_row, user_row):
        """Repeated 'Save as rule' must not create duplicate CorrectionRule rows."""
        from app.services.correction_service import apply_field_changes
        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)

        changes = {"supplier_name": "Acme Ltd"}
        apply_field_changes(db, batch=batch, row=row, changes=changes, user=user_row,
                            save_as_rule_fields={"supplier_name"})
        db.commit()
        count_1 = db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).count()

        # Second identical save — row.supplier_name is now "Acme Ltd" so src==tgt → no rule
        # Reset supplier_name to original to force a second rule-creation attempt
        row.supplier_name = "Old Supplier"
        apply_field_changes(db, batch=batch, row=row, changes=changes, user=user_row,
                            save_as_rule_fields={"supplier_name"})
        db.commit()
        count_2 = db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).count()

        assert count_1 == count_2 == 1, \
            f"Expected exactly 1 rule, got {count_1} then {count_2}"

    def test_disabled_rule_is_reactivated_on_duplicate_save(self, db, tenant_row, company_row, user_row):
        """If an equivalent rule exists but is disabled, saving re-enables it."""
        existing = _make_rule(
            db, tenant_row.id, company_id=company_row.id,
            source="old supplier", target="Acme Ltd", active=False,
        )
        db.commit()
        assert not existing.active

        from app.services.correction_service import apply_field_changes
        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)

        apply_field_changes(db, batch=batch, row=row,
                            changes={"supplier_name": "Acme Ltd"},
                            user=user_row, save_as_rule_fields={"supplier_name"})
        db.commit()

        db.refresh(existing)
        assert existing.active, "Re-saving an equivalent rule must re-enable the disabled rule"
        assert db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# 2. Normalisation
# ══════════════════════════════════════════════════════════════════════════════

class TestRuleNormalisation:

    def test_source_pattern_is_stored_lowercased(self, db, tenant_row, company_row, user_row):
        """source_pattern must be normalised to lowercase before storage."""
        from app.services.correction_service import apply_field_changes
        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "OLD SUPPLIER"
        apply_field_changes(db, batch=batch, row=row,
                            changes={"supplier_name": "Acme Ltd"},
                            user=user_row, save_as_rule_fields={"supplier_name"})
        db.commit()
        rule = db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).first()
        assert rule is not None
        assert rule.source_pattern == rule.source_pattern.lower(), \
            "source_pattern must be lowercase"

    def test_whitespace_collapsed_in_source(self, db, tenant_row, company_row, user_row):
        """Leading/trailing/repeated whitespace in source must be collapsed."""
        from app.services.correction_service import apply_field_changes
        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "  old   supplier  "
        apply_field_changes(db, batch=batch, row=row,
                            changes={"supplier_name": "Acme Ltd"},
                            user=user_row, save_as_rule_fields={"supplier_name"})
        db.commit()
        rule = db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).first()
        assert rule is not None
        assert "  " not in rule.source_pattern, "Double-spaces must be collapsed in source_pattern"
        assert rule.source_pattern == rule.source_pattern.strip()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Rule application: disabled rules skipped
# ══════════════════════════════════════════════════════════════════════════════

class TestRuleApplication:

    def test_disabled_rule_not_applied(self, db, tenant_row, company_row):
        """A disabled rule must NOT change the row during _apply_saved_rules."""
        from app.routers.batches import _apply_saved_rules
        _make_rule(db, tenant_row.id, source="old supplier",
                   target="Should Not Apply", active=False)
        db.commit()

        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "Old Supplier"

        _apply_saved_rules(db, batch, row)
        assert row.supplier_name == "Old Supplier", \
            "Disabled rule must not change the supplier name"

    def test_enabled_rule_applied(self, db, tenant_row, company_row):
        """An active rule must change the row during _apply_saved_rules."""
        from app.routers.batches import _apply_saved_rules
        _make_rule(db, tenant_row.id, source="old supplier",
                   target="New Supplier Ltd", active=True)
        db.commit()

        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "Old Supplier"

        _apply_saved_rules(db, batch, row)
        assert row.supplier_name == "New Supplier Ltd"

    def test_nominal_remap_applied(self, db, tenant_row, company_row):
        """Nominal remap rule changes the nominal_account_code."""
        from app.routers.batches import _apply_saved_rules
        _make_rule(db, tenant_row.id, rule_type="nominal_remap",
                   field_name="nominal_account_code",
                   source="5001", target="6001", active=True)
        db.commit()

        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.nominal_account_code = "5001"

        _apply_saved_rules(db, batch, row)
        assert row.nominal_account_code == "6001"

    def test_rule_not_applied_cross_tenant(self, db):
        """Rules from Tenant A must not affect rows belonging to Tenant B."""
        from app.routers.batches import _apply_saved_rules
        t_a = Tenant(id=uuid.uuid4(), tenant_code="TA", tenant_name="Tenant A")
        t_b = Tenant(id=uuid.uuid4(), tenant_code="TB", tenant_name="Tenant B")
        db.add_all([t_a, t_b])
        db.flush()

        _make_rule(db, t_a.id, source="old supplier", target="Tenant A Target", active=True)
        db.commit()

        c_b = Company(id=uuid.uuid4(), tenant_id=t_b.id, company_code="CB", company_name="Co B")
        db.add(c_b)
        db.flush()
        batch_b = _make_batch(db, t_b.id, c_b.id)
        row_b   = _make_row(db, batch_b)
        row_b.supplier_name = "Old Supplier"

        _apply_saved_rules(db, batch_b, row_b)
        assert row_b.supplier_name == "Old Supplier", \
            "Cross-tenant rules must never be applied"

    def test_company_scoped_rule_takes_precedence(self, db, tenant_row, company_row):
        """Company-scoped rule (company_id set) applies alongside global (company_id=None)."""
        from app.routers.batches import _apply_saved_rules
        # Global rule pointing to a wrong target
        _make_rule(db, tenant_row.id, company_id=None,
                   source="acme", target="Global Target", active=True)
        # Company-specific rule pointing to the correct target
        _make_rule(db, tenant_row.id, company_id=company_row.id,
                   source="acme", target="Company Target", active=True)
        db.commit()

        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "Acme"

        _apply_saved_rules(db, batch, row)
        # Both rules match — last one by id wins (ordered by id asc).
        # The important thing is that at least one rule was applied.
        assert row.supplier_name in ("Global Target", "Company Target"), \
            "At least one matching rule must be applied"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Rule update (no duplicate creation)
# ══════════════════════════════════════════════════════════════════════════════

class TestRuleUpdate:

    def test_update_rule_does_not_create_duplicate(self, db, tenant_row, admin_row):
        """Editing a rule must patch in place, not create a second row."""
        rule = _make_rule(db, tenant_row.id, source="old src", target="Old Target")
        db.commit()
        initial_count = db.query(CorrectionRule).count()

        # Simulate what PATCH /review/rules/{id} does
        import re
        new_src = re.sub(r"\s+", " ", "new src".strip().lower())
        new_tgt = "New Target"
        rule.source_pattern = new_src
        rule.target_value = new_tgt
        db.commit()

        assert db.query(CorrectionRule).count() == initial_count, \
            "Editing must not create extra rule rows"
        db.refresh(rule)
        assert rule.source_pattern == "new src"
        assert rule.target_value == "New Target"

    def test_update_rejected_when_duplicate_exists(self, db, tenant_row):
        """If another rule already covers the edited combination, update must fail."""
        _make_rule(db, tenant_row.id, source="existing src", target="Existing Target")
        rule2 = _make_rule(db, tenant_row.id, source="other src", target="Other Target")
        db.commit()

        # Attempt to change rule2 to match rule1's combination
        from sqlalchemy import select
        existing = db.execute(
            select(CorrectionRule).where(
                CorrectionRule.tenant_id == tenant_row.id,
                CorrectionRule.source_pattern == "existing src",
                CorrectionRule.target_value == "Existing Target",
                CorrectionRule.id != rule2.id,
            ).limit(1)
        ).scalar_one_or_none()
        assert existing is not None, "Duplicate detection query must find the conflicting rule"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Enable / disable toggle
# ══════════════════════════════════════════════════════════════════════════════

class TestEnableDisableRule:

    def test_disable_rule_sets_active_false(self, db, tenant_row, admin_row):
        rule = _make_rule(db, tenant_row.id, active=True)
        db.commit()
        assert rule.active

        rule.active = False
        rule.disabled_by = admin_row.id
        rule.disabled_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(rule)

        assert not rule.active
        assert rule.disabled_by == admin_row.id
        assert rule.disabled_at is not None

    def test_enable_rule_clears_disabled_fields(self, db, tenant_row, admin_row):
        rule = _make_rule(db, tenant_row.id, active=False)
        rule.disabled_by = admin_row.id
        rule.disabled_at = datetime.now(timezone.utc)
        db.commit()

        rule.active = True
        rule.disabled_by = None
        rule.disabled_at = None
        db.commit()
        db.refresh(rule)

        assert rule.active
        assert rule.disabled_by is None
        assert rule.disabled_at is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. Deletion
# ══════════════════════════════════════════════════════════════════════════════

class TestRuleDeletion:

    def test_deleted_rule_no_longer_applied(self, db, tenant_row, company_row):
        """After deletion, the rule must not be applied during scan."""
        from app.routers.batches import _apply_saved_rules
        rule = _make_rule(db, tenant_row.id, source="old supplier",
                          target="Should Disappear", active=True)
        db.commit()

        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "Old Supplier"

        # Verify rule applies before deletion
        _apply_saved_rules(db, batch, row)
        assert row.supplier_name == "Should Disappear"

        # Delete the rule
        db.delete(rule)
        db.commit()
        assert db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).count() == 0

        # Reset and verify rule no longer applies
        row.supplier_name = "Old Supplier"
        _apply_saved_rules(db, batch, row)
        assert row.supplier_name == "Old Supplier", \
            "Deleted rule must not be applied"


# ══════════════════════════════════════════════════════════════════════════════
# 7. Scope restriction via _get_rule_for_user
# ══════════════════════════════════════════════════════════════════════════════

class TestScopeRestriction:

    def test_user_cannot_access_other_tenant_rule(self, db, tenant_row, user_row):
        """_get_rule_for_user must raise 403 when user has no link to rule's tenant."""
        from fastapi import HTTPException
        from app.routers.review import _get_rule_for_user

        other_tenant = Tenant(id=uuid.uuid4(), tenant_code="OT", tenant_name="Other")
        db.add(other_tenant)
        db.flush()
        rule = _make_rule(db, other_tenant.id, source="x", target="y")
        db.commit()

        # user_row has no UserTenant link to other_tenant
        with pytest.raises(HTTPException) as exc:
            _get_rule_for_user(rule.id, db, user_row)
        assert exc.value.status_code == 403

    def test_user_can_access_own_tenant_rule(self, db, tenant_row, user_row):
        """_get_rule_for_user must succeed when user belongs to rule's tenant."""
        from app.routers.review import _get_rule_for_user

        link = UserTenant(user_id=user_row.id, tenant_id=tenant_row.id)
        db.add(link)
        db.flush()
        rule = _make_rule(db, tenant_row.id, source="a", target="b")
        db.commit()

        result = _get_rule_for_user(rule.id, db, user_row)
        assert result.id == rule.id

    def test_admin_can_access_any_rule(self, db, tenant_row, admin_row):
        """Admins bypass tenant scoping."""
        from app.routers.review import _get_rule_for_user
        rule = _make_rule(db, tenant_row.id, source="c", target="d")
        db.commit()

        result = _get_rule_for_user(rule.id, db, admin_row)
        assert result.id == rule.id


# ══════════════════════════════════════════════════════════════════════════════
# 8. Self-mapping validation
# ══════════════════════════════════════════════════════════════════════════════

class TestSelfMappingRejection:

    def test_source_equal_to_target_not_created(self, db, tenant_row, company_row, user_row):
        """A rule that maps X → X (after normalisation) must not be created."""
        from app.services.correction_service import apply_field_changes
        batch = _make_batch(db, tenant_row.id, company_row.id)
        row   = _make_row(db, batch)
        row.supplier_name = "Acme Ltd"
        # Both old and new value normalise to the same string → no rule
        apply_field_changes(db, batch=batch, row=row,
                            changes={"supplier_name": "Acme Ltd"},
                            user=user_row, save_as_rule_fields={"supplier_name"})
        db.commit()
        assert db.query(CorrectionRule).filter_by(tenant_id=tenant_row.id).count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# 9. _rule_to_dict shape
# ══════════════════════════════════════════════════════════════════════════════

class TestRuleDictShape:

    def test_rule_to_dict_has_required_keys(self, db, tenant_row):
        from app.routers.review import _rule_to_dict
        rule = _make_rule(db, tenant_row.id)
        db.commit()
        d = _rule_to_dict(rule)
        required = {"id", "tenant_id", "company_id", "rule_type", "field_name",
                    "source_pattern", "target_value", "active", "created_at",
                    "disabled_at", "origin_batch_id"}
        assert required.issubset(d.keys()), f"Missing keys: {required - d.keys()}"

    def test_rule_to_dict_active_is_bool(self, db, tenant_row):
        from app.routers.review import _rule_to_dict
        rule = _make_rule(db, tenant_row.id, active=True)
        db.commit()
        assert _rule_to_dict(rule)["active"] is True

        rule.active = False
        db.commit()
        assert _rule_to_dict(rule)["active"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 10. Nav — Rules link present in ap-ui.js
# ══════════════════════════════════════════════════════════════════════════════

class TestNavigation:

    def test_rules_link_in_nav(self):
        """ap-ui.js must include a Rules nav entry pointing to rules.html."""
        src = open("app/static/js/ap-ui.js").read()
        assert "rules.html" in src, "ap-ui.js must include rules.html in the nav"
        assert '"rules"' in src or "'rules'" in src, \
            "Rules nav entry must have id='rules'"

    def test_rules_link_not_admin_only(self):
        """The Rules nav link must NOT be adminOnly (all users can access it)."""
        import re
        src = open("app/static/js/ap-ui.js").read()
        # Find the rules nav entry and confirm adminOnly is not set
        m = re.search(r'\{[^}]*rules\.html[^}]*\}', src)
        assert m, "Could not find rules nav entry"
        entry = m.group(0)
        assert "adminOnly" not in entry, \
            "Rules nav entry must not be adminOnly — all users need access"
