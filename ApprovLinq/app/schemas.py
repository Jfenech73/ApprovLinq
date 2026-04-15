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
    status: str = Field(default="active")
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
    scan_mode: str = "summary"
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
    company_id: UUID
    supplier_account_code: str = Field(min_length=1, max_length=100)
    supplier_name: str = Field(min_length=1, max_length=255)
    default_nominal: str | None = Field(default=None, max_length=100)
    posting_account: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class SupplierUpdate(BaseModel):
    supplier_account_code: str | None = None
    supplier_name: str | None = None
    default_nominal: str | None = None
    posting_account: str | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    id: int
    tenant_id: UUID
    company_id: UUID
    supplier_account_code: str | None = None
    supplier_name: str
    default_nominal: str | None = None
    posting_account: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NominalAccountCreate(BaseModel):
    company_id: UUID
    account_code: str = Field(min_length=1, max_length=100)
    account_name: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    is_default: bool = False


class NominalAccountUpdate(BaseModel):
    account_code: str | None = None
    account_name: str | None = None
    is_active: bool | None = None
    is_default: bool | None = None


class NominalAccountOut(BaseModel):
    id: int
    tenant_id: UUID
    company_id: UUID
    account_code: str
    account_name: str
    is_active: bool
    is_default: bool
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


class ExportTemplateColumnCreate(BaseModel):
    column_heading: str = Field(min_length=1, max_length=255)
    column_type: str = Field(pattern="^(mapped_field|static_text|empty_column|derived_value|conditional_value)$")
    source_field: str | None = Field(default=None, max_length=100)
    static_value: str | None = Field(default=None, max_length=500)
    transform_rule: str | None = Field(default=None, max_length=200)
    condition_rules: list | None = None
    column_order: int = Field(default=0, ge=0)
    is_active: bool = True
    notes: str | None = None


class ExportTemplateColumnUpdate(BaseModel):
    column_heading: str | None = Field(default=None, max_length=255)
    column_type: str | None = Field(default=None, pattern="^(mapped_field|static_text|empty_column|derived_value|conditional_value)$")
    source_field: str | None = None
    static_value: str | None = None
    transform_rule: str | None = None
    condition_rules: list | None = None
    column_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    notes: str | None = None


class ExportTemplateColumnOut(BaseModel):
    id: int
    template_id: UUID
    column_order: int
    column_heading: str
    column_type: str
    source_field: str | None = None
    static_value: str | None = None
    transform_rule: str | None = None
    condition_rules: list | None = None
    is_active: bool
    notes: str | None = None

    model_config = {"from_attributes": True}


class ExportTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    accounting_system: str | None = Field(default=None, max_length=100)
    version_label: str = Field(default="v1", max_length=50)
    is_active: bool = True
    is_system_default: bool = False


class ExportTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    accounting_system: str | None = Field(default=None, max_length=100)
    version_label: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None
    is_system_default: bool | None = None


class ExportTemplateOut(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    accounting_system: str | None = None
    version_label: str
    is_active: bool
    is_system_default: bool
    created_at: datetime
    updated_at: datetime
    created_by: UUID | None = None
    updated_by: UUID | None = None

    model_config = {"from_attributes": True}


class ExportTemplateDetailOut(ExportTemplateOut):
    columns: list[ExportTemplateColumnOut] = Field(default_factory=list)


class ColumnReorderItem(BaseModel):
    id: int
    column_order: int = Field(ge=0)


class TemplateAssignmentCreate(BaseModel):
    template_id: UUID
    tenant_id: UUID
    company_id: UUID | None = None
    is_active: bool = True


class TemplateAssignmentOut(BaseModel):
    id: int
    template_id: UUID
    tenant_id: UUID
    company_id: UUID | None = None
    is_active: bool
    assigned_at: datetime
    assigned_by: UUID | None = None

    model_config = {"from_attributes": True}


class AdminAuditLogOut(BaseModel):
    id: int
    event_type: str
    entity_type: str
    entity_id: str | None = None
    user_id: UUID | None = None
    notes: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TemplatePreviewOut(BaseModel):
    sheet_name: str
    columns: list[str]
    sample_rows: list[dict]


class BatchCreate(BaseModel):
    batch_name: str = Field(min_length=1, max_length=255)
    company_id: UUID
    scan_mode: str = Field(default="summary", pattern="^(summary|lines)$")


class BatchUpdate(BaseModel):
    scan_mode: str | None = Field(default=None, pattern="^(summary|lines)$")
    batch_name: str | None = Field(default=None, min_length=1, max_length=255)


class BatchOut(BaseModel):
    id: UUID
    tenant_id: UUID | None = None
    company_id: UUID | None = None
    batch_name: str
    source_filename: str | None = None
    status: str
    page_count: int | None = None
    notes: str | None = None
    scan_mode: str = "summary"
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
    review_priority: str | None = None
    review_reasons: str | None = None
    review_fields: str | None = None
    auto_approved: bool = False
    page_quality_score: float | None = None
    classification_method: str | None = None
    supplier_match_method: str | None = None
    totals_reconciliation_status: str | None = None
    header_raw: str | None = None
    totals_raw: str | None = None
    page_text_raw: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
