"""Review/correction layer models. Imported by app.db.models via __init__ side effect.

Kept in a separate file to avoid editing the existing models.py. The InvoiceBatch
additive columns are attached dynamically below using SQLAlchemy's add_to_class
pattern via direct setattr on the existing mapped class — safe because columns
are nullable / defaulted.
"""
from __future__ import annotations
import uuid
from datetime import datetime, date
from sqlalchemy import (
    Boolean, DateTime, Date, ForeignKey, Integer, BigInteger, Numeric, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.models import Base, InvoiceBatch  # noqa: F401


def _utcnow() -> datetime:
    return datetime.utcnow()


CORRECTABLE_FIELDS: tuple[str, ...] = (
    "supplier_name", "supplier_posting_account", "nominal_account_code",
    "invoice_number", "invoice_date", "description",
    "net_amount", "vat_amount", "total_amount", "currency", "tax_code",
)


class InvoiceRowCorrection(Base):
    __tablename__ = "invoice_row_corrections"
    row_id: Mapped[int] = mapped_column(ForeignKey("invoice_rows.id", ondelete="CASCADE"), primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(Text)
    supplier_posting_account: Mapped[str | None] = mapped_column(String(100))
    nominal_account_code: Mapped[str | None] = mapped_column(String(100))
    invoice_number: Mapped[str | None] = mapped_column(Text)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(20))
    tax_code: Mapped[str | None] = mapped_column(String(50))
    reviewed_fields: Mapped[str | None] = mapped_column(Text)
    row_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class InvoiceRowFieldAudit(Base):
    __tablename__ = "invoice_row_field_audits"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    rule_created: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    force_added: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class CorrectionRule(Base):
    __tablename__ = "correction_rules"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=True)
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    source_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    target_value: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    disabled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin_batch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    origin_row_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class RemapHint(Base):
    __tablename__ = "remap_hints"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tenant_suppliers.id", ondelete="CASCADE"), nullable=True)
    supplier_name_snapshot: Mapped[str | None] = mapped_column(Text)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer)
    x: Mapped[float | None] = mapped_column(Numeric(8, 4))
    y: Mapped[float | None] = mapped_column(Numeric(8, 4))
    w: Mapped[float | None] = mapped_column(Numeric(8, 4))
    h: Mapped[float | None] = mapped_column(Numeric(8, 4))
    source_batch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source_file_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_row_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class BatchExportEvent(Base):
    __tablename__ = "batch_export_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    export_version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)


# Attach additive columns to InvoiceBatch dynamically (declarative add_to_class).
# These are nullable / defaulted, so adding them post-mapping is safe.
def _ensure_batch_columns() -> None:
    cols = {
        "approved_at":  mapped_column(DateTime(timezone=True), nullable=True),
        "approved_by":  mapped_column(ForeignKey("users.id"), nullable=True),
        "exported_at":  mapped_column(DateTime(timezone=True), nullable=True),
        "exported_by":  mapped_column(ForeignKey("users.id"), nullable=True),
        "reopened_at":  mapped_column(DateTime(timezone=True), nullable=True),
        "reopened_by":  mapped_column(ForeignKey("users.id"), nullable=True),
        "current_export_version": mapped_column(Integer, nullable=False, default=0, server_default="0"),
    }
    for name, col in cols.items():
        if not hasattr(InvoiceBatch, name):
            try:
                InvoiceBatch.__table__.append_column(col.column)  # type: ignore[attr-defined]
            except Exception:
                pass


_ensure_batch_columns()
