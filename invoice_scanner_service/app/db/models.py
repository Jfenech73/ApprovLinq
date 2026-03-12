import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Numeric, Boolean, Date, DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class InvoiceBatch(Base):
    __tablename__ = "invoice_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    batch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(50), default="created", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows: Mapped[list["InvoiceRow"]] = relationship(back_populates="batch", cascade="all, delete-orphan")

class InvoiceRow(Base):
    __tablename__ = "invoice_rows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(Text)
    invoice_number: Mapped[str | None] = mapped_column(Text)
    invoice_date: Mapped[datetime.date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    net_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    vat_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(20))
    tax_code: Mapped[str | None] = mapped_column(String(50))
    method_used: Mapped[str | None] = mapped_column(String(50))
    confidence_score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    validation_status: Mapped[str | None] = mapped_column(String(100))
    review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    header_raw: Mapped[str | None] = mapped_column(Text)
    totals_raw: Mapped[str | None] = mapped_column(Text)
    page_text_raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    batch: Mapped["InvoiceBatch"] = relationship(back_populates="rows")
