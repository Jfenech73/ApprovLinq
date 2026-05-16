"""add invoice read header/detail tables

Revision ID: 20260516_0003
Revises: 20260513_0002
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260516_0003"
down_revision = "20260513_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "invoice_read_headers" not in tables:
        op.create_table(
            "invoice_read_headers",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_file_id", sa.BigInteger(), sa.ForeignKey("invoice_files.id", ondelete="SET NULL"), nullable=True),
            sa.Column("row_id", sa.BigInteger(), sa.ForeignKey("invoice_rows.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_filename", sa.String(500), nullable=True),
            sa.Column("page_no", sa.Integer(), nullable=False),
            sa.Column("provider_name", sa.String(80), nullable=False),
            sa.Column("extraction_source", sa.String(80), nullable=True),
            sa.Column("method_used", sa.Text(), nullable=True),
            sa.Column("baseline_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("document_type", sa.String(80), nullable=True),
            sa.Column("document_confidence", sa.Numeric(6, 4), nullable=True),
            sa.Column("supplier_name", sa.Text(), nullable=True),
            sa.Column("supplier_vat", sa.String(100), nullable=True),
            sa.Column("supplier_address", sa.Text(), nullable=True),
            sa.Column("supplier_address_recipient", sa.Text(), nullable=True),
            sa.Column("customer_name", sa.Text(), nullable=True),
            sa.Column("customer_vat", sa.String(100), nullable=True),
            sa.Column("customer_address", sa.Text(), nullable=True),
            sa.Column("customer_address_recipient", sa.Text(), nullable=True),
            sa.Column("invoice_number", sa.Text(), nullable=True),
            sa.Column("invoice_date", sa.String(80), nullable=True),
            sa.Column("due_date", sa.String(80), nullable=True),
            sa.Column("order_number", sa.String(120), nullable=True),
            sa.Column("purchase_order", sa.String(120), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("net_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("vat_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("total_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("currency", sa.String(20), nullable=True),
            sa.Column("header_text", sa.Text(), nullable=True),
            sa.Column("totals_text", sa.Text(), nullable=True),
            sa.Column("page_text", sa.Text(), nullable=True),
            sa.Column("raw_provider_fields", sa.JSON(), nullable=True),
            sa.Column("raw_provider_payload", sa.JSON(), nullable=True),
            sa.Column("raw_di_fields", sa.JSON(), nullable=True),
            sa.Column("raw_di_payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_invoice_read_headers_batch_page", "invoice_read_headers", ["batch_id", "page_no"])
        op.create_index("ix_invoice_read_headers_row", "invoice_read_headers", ["row_id"])
        op.create_index("ix_invoice_read_headers_provider", "invoice_read_headers", ["provider_name"])

    if "invoice_read_details" not in tables:
        op.create_table(
            "invoice_read_details",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("header_id", sa.BigInteger(), sa.ForeignKey("invoice_read_headers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("line_no", sa.Integer(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("quantity", sa.Numeric(14, 4), nullable=True),
            sa.Column("unit_price", sa.Numeric(14, 4), nullable=True),
            sa.Column("net_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("tax_amount", sa.Numeric(14, 2), nullable=True),
            sa.Column("raw_detail", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_invoice_read_details_header", "invoice_read_details", ["header_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "invoice_read_details" in tables:
        op.drop_index("ix_invoice_read_details_header", table_name="invoice_read_details")
        op.drop_table("invoice_read_details")
    if "invoice_read_headers" in tables:
        op.drop_index("ix_invoice_read_headers_provider", table_name="invoice_read_headers")
        op.drop_index("ix_invoice_read_headers_row", table_name="invoice_read_headers")
        op.drop_index("ix_invoice_read_headers_batch_page", table_name="invoice_read_headers")
        op.drop_table("invoice_read_headers")
