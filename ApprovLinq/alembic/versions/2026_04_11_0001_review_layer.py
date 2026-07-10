"""review, audit, learning, remap layer

Revision ID: 20260411_0001
Revises:
Create Date: 2026-04-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from app.db.models import Base

revision = "20260411_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    insp = sa.inspect(bind)

    existing_batch_cols = {c["name"] for c in insp.get_columns("invoice_batches")}
    add = lambda name, col: op.add_column("invoice_batches", col) if name not in existing_batch_cols else None
    add("approved_at",  sa.Column("approved_at",  sa.DateTime(timezone=True), nullable=True))
    add("approved_by",  sa.Column("approved_by",  postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    add("exported_at",  sa.Column("exported_at",  sa.DateTime(timezone=True), nullable=True))
    add("exported_by",  sa.Column("exported_by",  postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    add("reopened_at",  sa.Column("reopened_at",  sa.DateTime(timezone=True), nullable=True))
    add("reopened_by",  sa.Column("reopened_by",  postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    add("current_export_version", sa.Column("current_export_version", sa.Integer(), nullable=False, server_default="0"))

    # Durable file storage for ephemeral hosts such as Koyeb.
    if "invoice_files" in insp.get_table_names():
        file_cols = {c["name"] for c in insp.get_columns("invoice_files")}
        if "file_bytes" not in file_cols:
            op.add_column("invoice_files", sa.Column("file_bytes", sa.LargeBinary(), nullable=True))
        if "storage_backend" not in file_cols:
            op.add_column("invoice_files", sa.Column("storage_backend", sa.String(30), nullable=False, server_default="database+local"))

    if "invoice_row_corrections" not in insp.get_table_names():
        op.create_table(
            "invoice_row_corrections",
            sa.Column("row_id", sa.BigInteger(), sa.ForeignKey("invoice_rows.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("supplier_name", sa.Text()),
            sa.Column("supplier_posting_account", sa.String(100)),
            sa.Column("nominal_account_code", sa.String(100)),
            sa.Column("invoice_number", sa.Text()),
            sa.Column("invoice_date", sa.Date()),
            sa.Column("description", sa.Text()),
            sa.Column("net_amount", sa.Numeric(14, 2)),
            sa.Column("vat_amount", sa.Numeric(14, 2)),
            sa.Column("total_amount", sa.Numeric(14, 2)),
            sa.Column("currency", sa.String(20)),
            sa.Column("tax_code", sa.String(50)),
            sa.Column("reviewed_fields", sa.Text()),
            sa.Column("row_reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        )
        op.create_index("ix_corrections_batch", "invoice_row_corrections", ["batch_id"])

    if "invoice_row_field_audits" not in insp.get_table_names():
        op.create_table(
            "invoice_row_field_audits",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("row_id", sa.BigInteger(), nullable=False),
            sa.Column("field_name", sa.String(80), nullable=False),
            sa.Column("old_value", sa.Text()),
            sa.Column("new_value", sa.Text()),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("note", sa.Text()),
            sa.Column("rule_created", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("force_added", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("username", sa.String(255)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_audits_batch_row", "invoice_row_field_audits", ["batch_id", "row_id"])

    if "correction_rules" not in insp.get_table_names():
        op.create_table(
            "correction_rules",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
            sa.Column("rule_type", sa.String(40), nullable=False),
            sa.Column("field_name", sa.String(80), nullable=False),
            sa.Column("source_pattern", sa.Text(), nullable=False),
            sa.Column("target_value", sa.Text(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("disabled_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("origin_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("origin_row_id", sa.BigInteger(), nullable=True),
            sa.Column("is_global", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_rules_lookup", "correction_rules",
                        ["tenant_id", "rule_type", "field_name", "source_pattern", "active"])
        op.create_index("ix_rules_global_lookup", "correction_rules",
                        ["is_global", "rule_type", "field_name", "source_pattern", "active"])
    else:
        cols = {c["name"] for c in insp.get_columns("correction_rules")}
        if "is_global" not in cols:
            op.add_column("correction_rules", sa.Column("is_global", sa.Boolean(), nullable=False, server_default=sa.false()))
        existing_indexes = {ix["name"] for ix in insp.get_indexes("correction_rules")}
        if "ix_rules_global_lookup" not in existing_indexes:
            op.create_index("ix_rules_global_lookup", "correction_rules",
                            ["is_global", "rule_type", "field_name", "source_pattern", "active"])

    if "remap_hints" not in insp.get_table_names():
        op.create_table(
            "remap_hints",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
            sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("tenant_suppliers.id", ondelete="CASCADE"), nullable=True),
            sa.Column("supplier_name_snapshot", sa.Text()),
            sa.Column("field_name", sa.String(80), nullable=False),
            sa.Column("page_no", sa.Integer()),
            sa.Column("x", sa.Numeric(8, 4)), sa.Column("y", sa.Numeric(8, 4)),
            sa.Column("w", sa.Numeric(8, 4)), sa.Column("h", sa.Numeric(8, 4)),
            sa.Column("source_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("source_file_id", sa.BigInteger(), nullable=True),
            sa.Column("source_row_id", sa.BigInteger(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("archived_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("superseded_by_hint_id", sa.BigInteger(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("last_used_row_id", sa.BigInteger(), nullable=True),
            sa.Column("last_used_page_no", sa.Integer(), nullable=True),
            sa.Column("last_read_text", sa.Text(), nullable=True),
            sa.Column("last_result", sa.Text(), nullable=True),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("conflict_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("apply_count", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_remap_lookup", "remap_hints", ["supplier_id", "field_name", "active"])
        op.create_index("ix_remap_governance", "remap_hints", ["tenant_id", "company_id", "supplier_id", "field_name", "active", "archived", "is_primary"])
        op.create_index("ix_remap_lifecycle", "remap_hints", ["active", "archived", "deleted_at"])

    if "batch_export_events" not in insp.get_table_names():
        op.create_table(
            "batch_export_events",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("export_version", sa.Integer(), nullable=False),
            sa.Column("template_id", sa.BigInteger(), nullable=True),
            sa.Column("exported_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("file_path", sa.Text()),
            sa.Column("file_bytes", sa.LargeBinary(), nullable=True),
            sa.Column("storage_backend", sa.String(30), nullable=False, server_default="database+local"),
            sa.Column("row_count", sa.Integer()),
        )
        op.create_index("ix_export_events_batch", "batch_export_events", ["batch_id"])
    else:
        export_cols = {c["name"] for c in insp.get_columns("batch_export_events")}
        if "file_bytes" not in export_cols:
            op.add_column("batch_export_events", sa.Column("file_bytes", sa.LargeBinary(), nullable=True))
        if "storage_backend" not in export_cols:
            op.add_column("batch_export_events", sa.Column("storage_backend", sa.String(30), nullable=False, server_default="database+local"))


def downgrade() -> None:
    op.drop_table("batch_export_events")
    op.drop_table("remap_hints")
    op.drop_table("correction_rules")
    op.drop_table("invoice_row_field_audits")
    op.drop_table("invoice_row_corrections")
    for col in ("current_export_version", "reopened_by", "reopened_at",
                "exported_by", "exported_at", "approved_by", "approved_at"):
        op.drop_column("invoice_batches", col)
