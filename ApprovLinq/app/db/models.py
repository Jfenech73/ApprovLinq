import uuid
from datetime import datetime, date, timezone

from sqlalchemy import (
    String,
    Text,
    Integer,
    Numeric,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    JSON,
    UniqueConstraint,
    LargeBinary,
    BigInteger,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


INVOICE_ROW_STATUS_ACTIVE = "active"
INVOICE_ROW_BLOCKED_STATUSES = frozenset({
    "blocked_duplicate",
    "blocked_false_positive",
    "superseded",
})
INVOICE_ROW_STATUSES = frozenset({INVOICE_ROW_STATUS_ACTIVE, *INVOICE_ROW_BLOCKED_STATUSES})

SCAN_JOB_STATUS_QUEUED = "queued"
SCAN_JOB_STATUS_CLAIMED = "claimed"
SCAN_JOB_STATUS_RUNNING = "running"
SCAN_JOB_STATUS_COMPLETED = "completed"
SCAN_JOB_STATUS_FAILED = "failed"
SCAN_JOB_STATUS_CANCELLED = "cancelled"
SCAN_JOB_ACTIVE_STATUSES = frozenset({
    SCAN_JOB_STATUS_QUEUED,
    SCAN_JOB_STATUS_CLAIMED,
    SCAN_JOB_STATUS_RUNNING,
})

SCAN_JOB_PAGE_STATUS_QUEUED = "queued"
SCAN_JOB_PAGE_STATUS_CLAIMED = "claimed"
SCAN_JOB_PAGE_STATUS_RUNNING = "running"
SCAN_JOB_PAGE_STATUS_COMPLETED = "completed"
SCAN_JOB_PAGE_STATUS_FAILED = "failed"
SCAN_JOB_PAGE_STATUS_SKIPPED = "skipped"


def invoice_row_export_active(row: object) -> bool:
    return (getattr(row, "row_status", None) or INVOICE_ROW_STATUS_ACTIVE) == INVOICE_ROW_STATUS_ACTIVE


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_mode: Mapped[str] = mapped_column(String(20), default="summary", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(30), default="tenant", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class UserTenant(Base):
    __tablename__ = "user_tenants"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    tenant_role: Mapped[str] = mapped_column(String(30), default="tenant_admin", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("tenant_id", "company_code", name="uq_tenant_company_code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_code: Mapped[str] = mapped_column(String(100), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    registration_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class TenantSupplier(Base):
    __tablename__ = "tenant_suppliers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "supplier_name", name="uq_tenant_company_supplier_name"),
        UniqueConstraint("tenant_id", "company_id", "supplier_account_code", name="uq_tenant_company_supplier_account_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    supplier_account_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(255), nullable=False)
    vat_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_nominal: Mapped[str | None] = mapped_column(String(100), nullable=True)
    posting_account: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class TenantNominalAccount(Base):
    __tablename__ = "tenant_nominal_accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "company_id", "account_code", name="uq_tenant_company_nominal_account_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    account_code: Mapped[str] = mapped_column(String(100), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class IssueLog(Base):
    __tablename__ = "issue_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="processing", nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    extractor_build_tag: Mapped[str | None] = mapped_column(String(120), nullable=True)
    scan_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    settings_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_backend: Mapped[str | None] = mapped_column(String(80), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    __table_args__ = (
        Index("ix_scan_jobs_status", "status"),
        Index("ix_scan_jobs_lease_until", "lease_until"),
        Index("ix_scan_jobs_batch", "batch_id"),
        Index("ix_scan_jobs_scan_run", "scan_run_id"),
        Index("ix_scan_jobs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default=SCAN_JOB_STATUS_QUEUED, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class ScanJobPage(Base):
    __tablename__ = "scan_job_pages"
    __table_args__ = (
        UniqueConstraint("scan_run_id", "source_file_id", "page_no", name="uq_scan_job_pages_run_file_page"),
        Index("ix_scan_job_pages_job", "job_id"),
        Index("ix_scan_job_pages_status", "status"),
        Index("ix_scan_job_pages_lease_until", "lease_until"),
        Index("ix_scan_job_pages_batch", "batch_id"),
        Index("ix_scan_job_pages_scan_run", "scan_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("scan_jobs.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False)
    source_file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("invoice_files.id", ondelete="CASCADE"), nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default=SCAN_JOB_PAGE_STATUS_QUEUED, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class InvoiceBatch(Base):
    __tablename__ = "invoice_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    batch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="created", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_mode: Mapped[str] = mapped_column(String(20), default="summary", nullable=False)
    current_scan_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rows: Mapped[list["InvoiceRow"]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    files: Mapped[list["InvoiceFile"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class InvoiceFile(Base):
    __tablename__ = "invoice_files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    # Durable copy for platforms with ephemeral filesystem storage (for example Koyeb).
    # The local file_path is still used as a runtime cache, but file_bytes is the
    # recovery source after a redeploy/restart removes local files.
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(30), default="database+local", nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="uploaded", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped["InvoiceBatch"] = relationship(back_populates="files")
    rows: Mapped[list["InvoiceRow"]] = relationship(back_populates="source_file")


class InvoiceRow(Base):
    __tablename__ = "invoice_rows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_files.id", ondelete="SET NULL"), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_posting_account: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nominal_account_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_items_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tax_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    method_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    review_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    row_status: Mapped[str] = mapped_column(String(40), default=INVOICE_ROW_STATUS_ACTIVE, nullable=False)
    row_status_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    row_status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_status_changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    page_quality_score: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    classification_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    supplier_match_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    totals_reconciliation_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    header_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    totals_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_text_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    batch: Mapped["InvoiceBatch"] = relationship(back_populates="rows")
    source_file: Mapped["InvoiceFile | None"] = relationship(back_populates="rows")
    read_headers: Mapped[list["InvoiceReadHeader"]] = relationship(back_populates="row")


class InvoiceReadHeader(Base):
    __tablename__ = "invoice_read_headers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_files.id", ondelete="SET NULL"), nullable=True)
    row_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_rows.id", ondelete="SET NULL"), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    extraction_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    method_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    document_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    document_confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_vat: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supplier_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_address_recipient: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_vat: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_address_recipient: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_date: Mapped[str | None] = mapped_column(String(80), nullable=True)
    due_date: Mapped[str | None] = mapped_column(String(80), nullable=True)
    order_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    purchase_order: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    header_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    totals_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_provider_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_provider_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_di_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_di_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    BatchPages: Mapped[int | None] = mapped_column("BatchPages", Integer, nullable=True)
    DocumentInBatch: Mapped[int | None] = mapped_column("DocumentInBatch", Integer, nullable=True)
    DocType: Mapped[str | None] = mapped_column("DocType", String(80), nullable=True)
    DocumentConfidence: Mapped[float | None] = mapped_column("DocumentConfidence", Numeric(6, 4), nullable=True)
    CustomerName: Mapped[str | None] = mapped_column("CustomerName", Text, nullable=True)
    CustomerId: Mapped[str | None] = mapped_column("CustomerId", String(120), nullable=True)
    PurchaseOrder: Mapped[str | None] = mapped_column("PurchaseOrder", String(120), nullable=True)
    InvoiceId: Mapped[str | None] = mapped_column("InvoiceId", Text, nullable=True)
    InvoiceDate: Mapped[str | None] = mapped_column("InvoiceDate", String(80), nullable=True)
    DueDate: Mapped[str | None] = mapped_column("DueDate", String(80), nullable=True)
    VendorName: Mapped[str | None] = mapped_column("VendorName", Text, nullable=True)
    VendorAddress: Mapped[str | None] = mapped_column("VendorAddress", Text, nullable=True)
    VendorAddressRecipient: Mapped[str | None] = mapped_column("VendorAddressRecipient", Text, nullable=True)
    CustomerAddress: Mapped[str | None] = mapped_column("CustomerAddress", Text, nullable=True)
    CustomerAddressRecipient: Mapped[str | None] = mapped_column("CustomerAddressRecipient", Text, nullable=True)
    BillingAddress: Mapped[str | None] = mapped_column("BillingAddress", Text, nullable=True)
    BillingAddressRecipient: Mapped[str | None] = mapped_column("BillingAddressRecipient", Text, nullable=True)
    ShippingAddress: Mapped[str | None] = mapped_column("ShippingAddress", Text, nullable=True)
    ShippingAddressRecipient: Mapped[str | None] = mapped_column("ShippingAddressRecipient", Text, nullable=True)
    SubTotal: Mapped[str | None] = mapped_column("SubTotal", Text, nullable=True)
    TotalDiscount: Mapped[str | None] = mapped_column("TotalDiscount", Text, nullable=True)
    TotalTax: Mapped[str | None] = mapped_column("TotalTax", Text, nullable=True)
    InvoiceTotal: Mapped[str | None] = mapped_column("InvoiceTotal", Text, nullable=True)
    AmountDue: Mapped[str | None] = mapped_column("AmountDue", Text, nullable=True)
    PreviousUnpaidBalance: Mapped[str | None] = mapped_column("PreviousUnpaidBalance", Text, nullable=True)
    RemittanceAddress: Mapped[str | None] = mapped_column("RemittanceAddress", Text, nullable=True)
    RemittanceAddressRecipient: Mapped[str | None] = mapped_column("RemittanceAddressRecipient", Text, nullable=True)
    ServiceAddress: Mapped[str | None] = mapped_column("ServiceAddress", Text, nullable=True)
    ServiceAddressRecipient: Mapped[str | None] = mapped_column("ServiceAddressRecipient", Text, nullable=True)
    ServiceStartDate: Mapped[str | None] = mapped_column("ServiceStartDate", String(80), nullable=True)
    ServiceEndDate: Mapped[str | None] = mapped_column("ServiceEndDate", String(80), nullable=True)
    VendorTaxId: Mapped[str | None] = mapped_column("VendorTaxId", String(120), nullable=True)
    CustomerTaxId: Mapped[str | None] = mapped_column("CustomerTaxId", String(120), nullable=True)
    PaymentTerm: Mapped[str | None] = mapped_column("PaymentTerm", Text, nullable=True)
    KVKNumber: Mapped[str | None] = mapped_column("KVKNumber", String(120), nullable=True)
    CurrencyCode: Mapped[str | None] = mapped_column("CurrencyCode", String(20), nullable=True)
    VendorPhoneNumber: Mapped[str | None] = mapped_column("VendorPhoneNumber", String(120), nullable=True)
    CustomerPhoneNumber: Mapped[str | None] = mapped_column("CustomerPhoneNumber", String(120), nullable=True)
    BillingPhoneNumber: Mapped[str | None] = mapped_column("BillingPhoneNumber", String(120), nullable=True)
    VendorEmail: Mapped[str | None] = mapped_column("VendorEmail", String(255), nullable=True)
    VendorFaxNumber: Mapped[str | None] = mapped_column("VendorFaxNumber", String(120), nullable=True)
    ReferenceNumber: Mapped[str | None] = mapped_column("ReferenceNumber", String(120), nullable=True)
    PaymentDetails: Mapped[dict | list | None] = mapped_column("PaymentDetails", JSON, nullable=True)
    TaxDetails: Mapped[dict | list | None] = mapped_column("TaxDetails", JSON, nullable=True)
    PaidInFourInstallements: Mapped[dict | list | None] = mapped_column("PaidInFourInstallements", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    row: Mapped["InvoiceRow | None"] = relationship(back_populates="read_headers")
    details: Mapped[list["InvoiceReadDetail"]] = relationship(back_populates="header", cascade="all, delete-orphan")


class InvoiceReadDetail(Base):
    __tablename__ = "invoice_read_details"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    header_id: Mapped[int] = mapped_column(ForeignKey("invoice_read_headers.id", ondelete="CASCADE"), nullable=False)
    line_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    di_amount: Mapped[str | None] = mapped_column(Text, nullable=True)
    di_date: Mapped[str | None] = mapped_column(String(80), nullable=True)
    di_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    di_product_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    di_quantity: Mapped[str | None] = mapped_column(String(80), nullable=True)
    di_tax: Mapped[str | None] = mapped_column(Text, nullable=True)
    di_tax_rate: Mapped[str | None] = mapped_column(String(80), nullable=True)
    di_unit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    di_unit_price: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    header: Mapped["InvoiceReadHeader"] = relationship(back_populates="details")


class ExportTemplate(Base):
    __tablename__ = "export_templates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    accounting_system: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version_label: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    columns: Mapped[list["ExportTemplateColumn"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="ExportTemplateColumn.column_order",
    )
    assignments: Mapped[list["TemplateAssignment"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
    )


class ExportTemplateColumn(Base):
    __tablename__ = "export_template_columns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("export_templates.id", ondelete="CASCADE"), nullable=False)
    column_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    column_heading: Mapped[str] = mapped_column(String(255), nullable=False)
    column_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    static_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    transform_rule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    condition_rules: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    template: Mapped["ExportTemplate"] = relationship(back_populates="columns")


class TemplateAssignment(Base):
    __tablename__ = "template_assignments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("export_templates.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    template: Mapped["ExportTemplate"] = relationship(back_populates="assignments")


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class SupplierPattern(Base):
    """Stores supplier keyword fingerprints with explicit trust lifecycle."""

    __tablename__ = "supplier_patterns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "supplier_id", name="uq_supplier_pattern"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_suppliers.id", ondelete="CASCADE"), nullable=False
    )
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    trusted_outcome_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_batch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source_row_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_trusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposed_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
