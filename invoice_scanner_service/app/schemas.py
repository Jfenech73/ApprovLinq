from datetime import date, datetime
from pydantic import BaseModel, Field
from uuid import UUID


class BatchCreate(BaseModel):
    batch_name: str = Field(min_length=1, max_length=255)


class BatchOut(BaseModel):
    id: UUID
    batch_name: str
    source_filename: str | None = None
    status: str
    page_count: int | None = None
    notes: str | None = None
    created_at: datetime
    processed_at: datetime | None = None

    model_config = {"from_attributes": True}


class BatchFileOut(BaseModel):
    id: int
    batch_id: UUID
    original_filename: str
    stored_filename: str
    mime_type: str | None = None
    status: str
    page_count: int | None = None
    error_message: str | None = None
    uploaded_at: datetime
    processed_at: datetime | None = None

    model_config = {"from_attributes": True}


class BatchDetailOut(BatchOut):
    uploaded_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    rows_count: int = 0
    files: list[BatchFileOut] = Field(default_factory=list)


class InvoiceRowOut(BaseModel):
    id: int
    batch_id: UUID
    source_file_id: int | None = None
    source_filename: str | None = None
    page_no: int
    supplier_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    description: str | None = None
    line_items_raw: str | None = None
    net_amount: float | None = None
    vat_amount: float | None = None
    total_amount: float | None = None
    currency: str | None = None
    tax_code: str | None = None
    method_used: str | None = None
    confidence_score: float | None = None
    validation_status: str | None = None
    review_required: bool
    header_raw: str | None = None
    totals_raw: str | None = None
    page_text_raw: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
