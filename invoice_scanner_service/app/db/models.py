import uuid
from datetime import datetime, date

from sqlalchemy import (
    String,
    Text,
    Integer,
    Numeric,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InvoiceBatch(Base):
    __tablename__ = "invoice_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    batch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="created", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    rows: Mapped[list["InvoiceRow"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
    )
    files: Mapped[list["InvoiceFile"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class InvoiceFile(Base):
    __tablename__ = "invoice_files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="uploaded", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    batch: Mapped["InvoiceBatch"] = relationship(back_populates="files")
    rows: Mapped[list["InvoiceRow"]] = relationship(back_populates="source_file")


class InvoiceRow(Base):
    __tablename__ = "invoice_rows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_items_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tax_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    method_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    header_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    totals_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_text_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    batch: Mapped["InvoiceBatch"] = relationship(back_populates="rows")
    source_file: Mapped["InvoiceFile | None"] = relationship(back_populates="rows")
