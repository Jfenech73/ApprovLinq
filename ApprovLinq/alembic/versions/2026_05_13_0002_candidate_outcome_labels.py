"""add outcome labels to invoice field candidates

Revision ID: 20260513_0002
Revises: 20260513_0001
Create Date: 2026-05-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260513_0002"
down_revision = "20260513_0001"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    cols = _column_names("invoice_field_candidates")
    if not cols:
        return
    if "user_accepted" not in cols:
        op.add_column("invoice_field_candidates", sa.Column("user_accepted", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "user_corrected" not in cols:
        op.add_column("invoice_field_candidates", sa.Column("user_corrected", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "final_value" not in cols:
        op.add_column("invoice_field_candidates", sa.Column("final_value", sa.Text(), nullable=True))
    if "finalised_at" not in cols:
        op.add_column("invoice_field_candidates", sa.Column("finalised_at", sa.DateTime(timezone=True), nullable=True))
    if "finalised_by" not in cols:
        op.add_column("invoice_field_candidates", sa.Column("finalised_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True))
    if "outcome_source" not in cols:
        op.add_column("invoice_field_candidates", sa.Column("outcome_source", sa.String(40), nullable=True))


def downgrade() -> None:
    cols = _column_names("invoice_field_candidates")
    for col in ("outcome_source", "finalised_by", "finalised_at", "final_value", "user_corrected", "user_accepted"):
        if col in cols:
            op.drop_column("invoice_field_candidates", col)
