"""add invoice field candidates table

Revision ID: 20260513_0001
Revises: 20260411_0001
Create Date: 2026-05-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260513_0001"
down_revision = "20260411_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "invoice_field_candidates" not in insp.get_table_names():
        op.create_table(
            "invoice_field_candidates",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("row_id", sa.BigInteger(), sa.ForeignKey("invoice_rows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_file_id", sa.BigInteger(), sa.ForeignKey("invoice_files.id", ondelete="SET NULL"), nullable=True),
            sa.Column("field_name", sa.String(80), nullable=False),
            sa.Column("candidate_value", sa.Text(), nullable=True),
            sa.Column("normalised_value", sa.Text(), nullable=True),
            sa.Column("source_type", sa.String(80), nullable=False),
            sa.Column("source_id", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Numeric(6, 4), nullable=True),
            sa.Column("evidence", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("rejected_reason", sa.Text(), nullable=True),
            sa.Column("conflict", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    existing_indexes = {ix["name"] for ix in insp.get_indexes("invoice_field_candidates")}
    indexes = {
        "ix_field_candidates_tenant_company": ["tenant_id", "company_id"],
        "ix_field_candidates_batch_row": ["batch_id", "row_id"],
        "ix_field_candidates_field_name": ["field_name"],
        "ix_field_candidates_source_type": ["source_type"],
        "ix_field_candidates_selected": ["selected"],
        "ix_field_candidates_created_at": ["created_at"],
    }
    for name, cols in indexes.items():
        if name not in existing_indexes:
            op.create_index(name, "invoice_field_candidates", cols)


def downgrade() -> None:
    op.drop_index("ix_field_candidates_created_at", table_name="invoice_field_candidates")
    op.drop_index("ix_field_candidates_selected", table_name="invoice_field_candidates")
    op.drop_index("ix_field_candidates_source_type", table_name="invoice_field_candidates")
    op.drop_index("ix_field_candidates_field_name", table_name="invoice_field_candidates")
    op.drop_index("ix_field_candidates_batch_row", table_name="invoice_field_candidates")
    op.drop_index("ix_field_candidates_tenant_company", table_name="invoice_field_candidates")
    op.drop_table("invoice_field_candidates")
