"""database migration foundation and DI detail field prefixes

Revision ID: 20260710_0009
Revises: 20260710_0008
Create Date: 2026-07-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260710_0009"
down_revision = "20260710_0008"
branch_labels = None
depends_on = None


LEGACY_DETAIL_COLUMNS = {
    "Amount": sa.Column("Amount", sa.Text(), nullable=True),
    "Date": sa.Column("Date", sa.String(length=80), nullable=True),
    "Description": sa.Column("Description", sa.Text(), nullable=True),
    "ProductCode": sa.Column("ProductCode", sa.String(length=120), nullable=True),
    "Quantity": sa.Column("Quantity", sa.String(length=80), nullable=True),
    "Tax": sa.Column("Tax", sa.Text(), nullable=True),
    "TaxRate": sa.Column("TaxRate", sa.String(length=80), nullable=True),
    "Unit": sa.Column("Unit", sa.String(length=80), nullable=True),
    "UnitPrice": sa.Column("UnitPrice", sa.Text(), nullable=True),
}

PREFIXED_DETAIL_COLUMNS = {
    "di_amount": sa.Column("di_amount", sa.Text(), nullable=True),
    "di_date": sa.Column("di_date", sa.String(length=80), nullable=True),
    "di_description": sa.Column("di_description", sa.Text(), nullable=True),
    "di_product_code": sa.Column("di_product_code", sa.String(length=120), nullable=True),
    "di_quantity": sa.Column("di_quantity", sa.String(length=80), nullable=True),
    "di_tax": sa.Column("di_tax", sa.Text(), nullable=True),
    "di_tax_rate": sa.Column("di_tax_rate", sa.String(length=80), nullable=True),
    "di_unit": sa.Column("di_unit", sa.String(length=80), nullable=True),
    "di_unit_price": sa.Column("di_unit_price", sa.Text(), nullable=True),
}

BACKFILL_PAIRS = (
    ("di_amount", "Amount"),
    ("di_date", "Date"),
    ("di_description", "Description"),
    ("di_product_code", "ProductCode"),
    ("di_quantity", "Quantity"),
    ("di_tax", "Tax"),
    ("di_tax_rate", "TaxRate"),
    ("di_unit", "Unit"),
    ("di_unit_price", "UnitPrice"),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column.copy())


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    if column_name in _columns(table_name):
        op.drop_column(table_name, column_name)


def _alter_column_type_if_present(table_name: str, column_name: str, type_: sa.types.TypeEngine) -> None:
    if column_name in _columns(table_name):
        op.alter_column(table_name, column_name, type_=type_, existing_nullable=True)


def upgrade() -> None:
    if "invoice_rows" in _tables():
        _alter_column_type_if_present("invoice_rows", "method_used", sa.Text())
        _alter_column_type_if_present("invoice_rows", "review_reasons", sa.Text())
        _alter_column_type_if_present("invoice_rows", "review_fields", sa.Text())

    if "invoice_read_details" not in _tables():
        return

    for column in PREFIXED_DETAIL_COLUMNS.values():
        _add_column_if_missing("invoice_read_details", column)

    existing = _columns("invoice_read_details")
    for prefixed, legacy in BACKFILL_PAIRS:
        if prefixed in existing and legacy in existing:
            op.execute(
                sa.text(
                    f'UPDATE invoice_read_details SET {prefixed} = "{legacy}" '
                    f'WHERE {prefixed} IS NULL AND "{legacy}" IS NOT NULL'
                )
            )

    for legacy in LEGACY_DETAIL_COLUMNS:
        _drop_column_if_present("invoice_read_details", legacy)


def downgrade() -> None:
    if "invoice_read_details" not in _tables():
        return

    for column in LEGACY_DETAIL_COLUMNS.values():
        _add_column_if_missing("invoice_read_details", column)

    existing = _columns("invoice_read_details")
    for prefixed, legacy in BACKFILL_PAIRS:
        if prefixed in existing and legacy in existing:
            op.execute(
                sa.text(
                    f'UPDATE invoice_read_details SET "{legacy}" = {prefixed} '
                    f'WHERE "{legacy}" IS NULL AND {prefixed} IS NOT NULL'
                )
            )

    for prefixed in PREFIXED_DETAIL_COLUMNS:
        _drop_column_if_present("invoice_read_details", prefixed)
