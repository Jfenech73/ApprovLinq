from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base
from app.db.review_models import _BIGINT_PK, _utcnow


class ApprovedInvoiceFact(Base):
    """Immutable approved/exported invoice fact used for expense insights."""

    __tablename__ = "approved_invoice_facts"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_row_id", "fact_version", name="uq_approved_fact_batch_row_version"),
        Index("ix_approved_facts_tenant_company_date", "tenant_id", "company_id", "invoice_date"),
        Index("ix_approved_facts_supplier", "tenant_id", "company_id", "canonical_supplier_name"),
        Index("ix_approved_facts_nominal", "tenant_id", "company_id", "nominal_account_code"),
        Index("ix_approved_facts_batch_version", "batch_id", "fact_version"),
        Index("ix_approved_facts_source_row", "source_row_id"),
        Index("ix_approved_facts_export_event", "export_event_id"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    source_row_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("invoice_rows.id", ondelete="CASCADE"), nullable=False)
    export_event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("batch_export_events.id", ondelete="SET NULL"), nullable=True)
    export_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_ref_type: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_ref_id: Mapped[str] = mapped_column(Text, nullable=False)

    canonical_supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_posting_account: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supplier_vat: Mapped[str | None] = mapped_column(String(100), nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    nominal_account_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nominal_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reporting_currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tax_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    reporting_net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    reporting_vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    reporting_total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    bcrs_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    deposit_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    source_row_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_validation_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    duplicate_exposure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_exposure_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
