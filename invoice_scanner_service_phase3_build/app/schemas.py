from datetime import date, datetime
from pydantic import BaseModel, Field, EmailStr
from uuid import UUID


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=255)


class TenantBrief(BaseModel):
    tenant_id: UUID
    tenant_name: str
    tenant_code: str
    tenant_role: str
    is_default: bool = False


class LoginResponse(BaseModel):
    access_token: str
    user_id: UUID
    email: EmailStr
    full_name: str
    role: str
    tenants: list[TenantBrief] = Field(default_factory=list)
    landing_page: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=255)


class TenantCreate(BaseModel):
    tenant_code: str = Field(min_length=2, max_length=100)
    tenant_name: str = Field(min_length=2, max_length=255)
    contact_name: str | None = None
    contact_email: EmailStr | None = None
    notes: str | None = None


class TenantUpdate(BaseModel):
    tenant_name: str | None = None
    status: str | None = None
    is_active: bool | None = None
    contact_name: str | None = None
    contact_email: EmailStr | None = None
    notes: str | None = None


class TenantOut(BaseModel):
    id: UUID
    tenant_code: str
    tenant_name: str
    status: str
    is_active: bool
    contact_name: str | None = None
    contact_email: str | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=8, max_length=255)
    role: str = Field(default="tenant")
    is_active: bool = True
    tenant_ids: list[UUID] = Field(default_factory=list)


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    tenant_ids: list[UUID] | None = None


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CompanyCreate(BaseModel):
    company_code: str = Field(min_length=2, max_length=100)
    company_name: str = Field(min_length=2, max_length=255)
    registration_number: str | None = None
    vat_number: str | None = None
    is_active: bool = True


class CompanyUpdate(BaseModel):
    company_name: str | None = None
    registration_number: str | None = None
    vat_number: str | None = None
    is_active: bool | None = None


class CompanyOut(BaseModel):
    id: UUID
    tenant_id: UUID
    company_code: str
    company_name: str
    registration_number: str | None = None
    vat_number: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SupplierCreate(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=255)
    posting_account: str = Field(min_length=1, max_length=100)
    is_active: bool = True


class SupplierUpdate(BaseModel):
    supplier_name: str | None = None
    posting_account: str | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    id: int
    tenant_id: UUID
    supplier_name: str
    posting_account: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NominalAccountCreate(BaseModel):
    account_code: str = Field(min_length=1, max_length=100)
    account_name: str = Field(min_length=1, max_length=255)
    is_active: bool = True


class NominalAccountUpdate(BaseModel):
    account_code: str | None = None
    account_name: str | None = None
    is_active: bool | None = None


class NominalAccountOut(BaseModel):
    id: int
    tenant_id: UUID
    account_code: str
    account_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IssueCreate(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    description: str = Field(min_length=5)
    priority: str = Field(default="normal")


class IssueUpdate(BaseModel):
    status: str | None = None
    resolution_notes: str | None = None
    priority: str | None = None


class IssueOut(BaseModel):
    id: int
    tenant_id: UUID
    created_by_user_id: UUID | None = None
    title: str
    description: str
    status: str
    priority: str
    resolution_notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CapacityRow(BaseModel):
    tenant_id: UUID
    tenant_name: str
    tenant_code: str
    status: str
    companies: int
    batches: int
    files: int
    rows: int
    storage_mb: float


class BatchCreate(BaseModel):
    batch_name: str = Field(min_length=1, max_length=255)
    company_id: UUID


class BatchOut(BaseModel):
    id: UUID
    tenant_id: UUID | None = None
    company_id: UUID | None = None
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
    tenant_id: UUID | None = None
    company_id: UUID | None = None
    original_filename: str
    stored_filename: str
    mime_type: str | None = None
    file_size_bytes: int | None = None
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
    tenant_id: UUID | None = None
    company_id: UUID | None = None
    source_file_id: int | None = None
    source_filename: str | None = None
    page_no: int
    supplier_name: str | None = None
    supplier_posting_account: str | None = None
    nominal_account_code: str | None = None
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
