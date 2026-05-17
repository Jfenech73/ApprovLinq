"""add microsoft di named columns to invoice read tables

Revision ID: 20260516_0004
Revises: 20260516_0003
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa

revision = "20260516_0004"
down_revision = "20260516_0003"
branch_labels = None
depends_on = None


def _ensure_columns(table_name: str, columns: list[sa.Column]) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {col["name"] for col in insp.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "invoice_read_headers" in tables:
        _ensure_columns("invoice_read_headers", [
            sa.Column("BatchPages", sa.Integer(), nullable=True),
            sa.Column("DocumentInBatch", sa.Integer(), nullable=True),
            sa.Column("DocType", sa.String(length=80), nullable=True),
            sa.Column("DocumentConfidence", sa.Numeric(6, 4), nullable=True),
            sa.Column("CustomerName", sa.Text(), nullable=True),
            sa.Column("CustomerId", sa.String(length=120), nullable=True),
            sa.Column("PurchaseOrder", sa.String(length=120), nullable=True),
            sa.Column("InvoiceId", sa.Text(), nullable=True),
            sa.Column("InvoiceDate", sa.String(length=80), nullable=True),
            sa.Column("DueDate", sa.String(length=80), nullable=True),
            sa.Column("VendorName", sa.Text(), nullable=True),
            sa.Column("VendorAddress", sa.Text(), nullable=True),
            sa.Column("VendorAddressRecipient", sa.Text(), nullable=True),
            sa.Column("CustomerAddress", sa.Text(), nullable=True),
            sa.Column("CustomerAddressRecipient", sa.Text(), nullable=True),
            sa.Column("BillingAddress", sa.Text(), nullable=True),
            sa.Column("BillingAddressRecipient", sa.Text(), nullable=True),
            sa.Column("ShippingAddress", sa.Text(), nullable=True),
            sa.Column("ShippingAddressRecipient", sa.Text(), nullable=True),
            sa.Column("SubTotal", sa.Text(), nullable=True),
            sa.Column("TotalDiscount", sa.Text(), nullable=True),
            sa.Column("TotalTax", sa.Text(), nullable=True),
            sa.Column("InvoiceTotal", sa.Text(), nullable=True),
            sa.Column("AmountDue", sa.Text(), nullable=True),
            sa.Column("PreviousUnpaidBalance", sa.Text(), nullable=True),
            sa.Column("RemittanceAddress", sa.Text(), nullable=True),
            sa.Column("RemittanceAddressRecipient", sa.Text(), nullable=True),
            sa.Column("ServiceAddress", sa.Text(), nullable=True),
            sa.Column("ServiceAddressRecipient", sa.Text(), nullable=True),
            sa.Column("ServiceStartDate", sa.String(length=80), nullable=True),
            sa.Column("ServiceEndDate", sa.String(length=80), nullable=True),
            sa.Column("VendorTaxId", sa.String(length=120), nullable=True),
            sa.Column("CustomerTaxId", sa.String(length=120), nullable=True),
            sa.Column("PaymentTerm", sa.Text(), nullable=True),
            sa.Column("KVKNumber", sa.String(length=120), nullable=True),
            sa.Column("CurrencyCode", sa.String(length=20), nullable=True),
            sa.Column("VendorPhoneNumber", sa.String(length=120), nullable=True),
            sa.Column("CustomerPhoneNumber", sa.String(length=120), nullable=True),
            sa.Column("BillingPhoneNumber", sa.String(length=120), nullable=True),
            sa.Column("VendorEmail", sa.String(length=255), nullable=True),
            sa.Column("VendorFaxNumber", sa.String(length=120), nullable=True),
            sa.Column("ReferenceNumber", sa.String(length=120), nullable=True),
            sa.Column("PaymentDetails", sa.JSON(), nullable=True),
            sa.Column("TaxDetails", sa.JSON(), nullable=True),
            sa.Column("PaidInFourInstallements", sa.JSON(), nullable=True),
        ])
    if "invoice_read_details" in tables:
        _ensure_columns("invoice_read_details", [
            sa.Column("Amount", sa.Text(), nullable=True),
            sa.Column("Date", sa.String(length=80), nullable=True),
            sa.Column("Description", sa.Text(), nullable=True),
            sa.Column("ProductCode", sa.String(length=120), nullable=True),
            sa.Column("Quantity", sa.String(length=80), nullable=True),
            sa.Column("Tax", sa.Text(), nullable=True),
            sa.Column("TaxRate", sa.String(length=80), nullable=True),
            sa.Column("Unit", sa.String(length=80), nullable=True),
            sa.Column("UnitPrice", sa.Text(), nullable=True),
        ])


def downgrade() -> None:
    pass
