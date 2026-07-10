-- =============================================================================
-- ApprovLinq Invoice Scanner Service — Complete Database Schema
-- Safe to run on a fresh database OR an existing one.
-- All statements are idempotent: CREATE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS.
-- NO dollar-quoted DO blocks — compatible with all SQL editors including Neon.
-- =============================================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- TENANTS
-- ---------------------------------------------------------------------------
create table if not exists tenants (
    id           uuid        primary key default gen_random_uuid(),
    tenant_code  varchar(100) unique not null,
    tenant_name  varchar(255) not null,
    status       varchar(30)  not null default 'active',
    is_active    boolean      not null default true,
    contact_name varchar(255),
    contact_email varchar(255),
    notes        text,
    scan_mode    varchar(20)  not null default 'summary',
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now()
);

-- Back-fill column added after initial release
alter table tenants add column if not exists scan_mode varchar(20) not null default 'summary';


-- ---------------------------------------------------------------------------
-- USERS
-- ---------------------------------------------------------------------------
create table if not exists users (
    id            uuid        primary key default gen_random_uuid(),
    email         varchar(255) unique not null,
    full_name     varchar(255) not null,
    password_hash text         not null,
    role          varchar(30)  not null default 'tenant',
    is_active     boolean      not null default true,
    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);


-- ---------------------------------------------------------------------------
-- USER ↔ TENANT ASSIGNMENTS
-- ---------------------------------------------------------------------------
create table if not exists user_tenants (
    id          bigserial    primary key,
    user_id     uuid         not null references users(id)   on delete cascade,
    tenant_id   uuid         not null references tenants(id) on delete cascade,
    tenant_role varchar(30)  not null default 'tenant_admin',
    is_default  boolean      not null default false,
    created_at  timestamptz  not null default now(),
    constraint uq_user_tenant unique (user_id, tenant_id)
);


-- ---------------------------------------------------------------------------
-- USER SESSIONS
-- ---------------------------------------------------------------------------
create table if not exists user_sessions (
    id         bigserial   primary key,
    user_id    uuid        not null references users(id) on delete cascade,
    token_hash varchar(64) unique not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    revoked_at timestamptz
);


-- ---------------------------------------------------------------------------
-- COMPANIES  (one tenant can have multiple legal entities / company books)
-- ---------------------------------------------------------------------------
create table if not exists companies (
    id                  uuid        primary key default gen_random_uuid(),
    tenant_id           uuid        not null references tenants(id) on delete cascade,
    company_code        varchar(100) not null,
    company_name        varchar(255) not null,
    registration_number varchar(100),
    vat_number          varchar(100),
    is_active           boolean      not null default true,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    constraint uq_tenant_company_code unique (tenant_id, company_code)
);


-- ---------------------------------------------------------------------------
-- TENANT SUPPLIERS  (supplier master list per tenant + company)
-- ---------------------------------------------------------------------------
create table if not exists tenant_suppliers (
    id                    bigserial    primary key,
    tenant_id             uuid         not null references tenants(id)   on delete cascade,
    company_id            uuid         references companies(id) on delete cascade,
    supplier_account_code varchar(100),
    supplier_name         varchar(255) not null,
    default_nominal       varchar(100),
    posting_account       varchar(100) not null default '',
    is_active             boolean      not null default true,
    created_at            timestamptz  not null default now(),
    updated_at            timestamptz  not null default now()
);

-- Back-fill columns added after initial release (safe on existing DBs)
alter table tenant_suppliers add column if not exists company_id           uuid references companies(id) on delete cascade;
alter table tenant_suppliers add column if not exists supplier_account_code varchar(100);
alter table tenant_suppliers add column if not exists default_nominal       varchar(100);

-- Partial unique index on account code (NULL rows excluded)
create unique index if not exists ix_tenant_suppliers_tenant_company_account_code
    on tenant_suppliers(tenant_id, company_id, supplier_account_code)
    where supplier_account_code is not null;


-- ---------------------------------------------------------------------------
-- TENANT NOMINAL ACCOUNTS  (chart of accounts per tenant + company)
-- ---------------------------------------------------------------------------
create table if not exists tenant_nominal_accounts (
    id           bigserial    primary key,
    tenant_id    uuid         not null references tenants(id)   on delete cascade,
    company_id   uuid         references companies(id) on delete cascade,
    account_code varchar(100) not null,
    account_name varchar(255) not null,
    is_active    boolean      not null default true,
    is_default   boolean      not null default false,
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now(),
    constraint uq_tenant_company_nominal_account_code
        unique (tenant_id, company_id, account_code)
);

-- Back-fill columns added after initial release
alter table tenant_nominal_accounts add column if not exists company_id  uuid references companies(id) on delete cascade;
alter table tenant_nominal_accounts add column if not exists is_default  boolean not null default false;


-- ---------------------------------------------------------------------------
-- ISSUE LOGS  (manual / auto-generated review flags)
-- ---------------------------------------------------------------------------
create table if not exists issue_logs (
    id                  bigserial   primary key,
    tenant_id           uuid        not null references tenants(id) on delete cascade,
    created_by_user_id  uuid        references users(id) on delete set null,
    title               varchar(255) not null,
    description         text         not null,
    status              varchar(30)  not null default 'pending',
    priority            varchar(20)  not null default 'normal',
    resolution_notes    text,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now()
);


-- ---------------------------------------------------------------------------
-- INVOICE BATCHES  (one batch = one uploaded PDF, potentially many pages)
-- ---------------------------------------------------------------------------
create table if not exists invoice_batches (
    id              uuid        primary key default gen_random_uuid(),
    tenant_id       uuid        references tenants(id)   on delete set null,
    company_id      uuid        references companies(id) on delete set null,
    batch_name      varchar(255) not null,
    source_filename varchar(500),
    status          varchar(50)  not null default 'created',
    page_count      integer,
    notes           text,
    scan_mode       varchar(20)  not null default 'summary',
    current_scan_run_id uuid,
    created_at      timestamptz  not null default now(),
    processed_at    timestamptz
);

-- Back-fill columns added after initial release
alter table invoice_batches add column if not exists tenant_id  uuid references tenants(id)   on delete set null;
alter table invoice_batches add column if not exists company_id uuid references companies(id) on delete set null;
alter table invoice_batches add column if not exists scan_mode  varchar(20) default 'summary';
alter table invoice_batches add column if not exists current_scan_run_id uuid;

-- Ensure scan_mode is never NULL on old rows
update invoice_batches set scan_mode = 'summary' where scan_mode is null;

create index if not exists idx_invoice_batches_tenant_id  on invoice_batches(tenant_id);
create index if not exists idx_invoice_batches_company_id on invoice_batches(company_id);


-- ---------------------------------------------------------------------------
-- SCAN RUNS  (immutable processing attempts and evidence identity)
-- ---------------------------------------------------------------------------
create table if not exists scan_runs (
    id                          uuid primary key default gen_random_uuid(),
    batch_id                    uuid not null references invoice_batches(id) on delete cascade,
    tenant_id                   uuid references tenants(id) on delete set null,
    company_id                  uuid references companies(id) on delete set null,
    run_number                  integer not null,
    parent_run_id               uuid references scan_runs(id) on delete set null,
    status                      varchar(40) not null default 'processing',
    app_version                 varchar(80),
    extractor_build_tag         varchar(120),
    scan_mode                   varchar(20),
    settings_fingerprint        varchar(64),
    provider_config_fingerprint varchar(64),
    selected_backend            varchar(80),
    page_count                  integer,
    row_count                   integer,
    notes                       text,
    error_message               text,
    started_at                  timestamptz not null default now(),
    completed_at                timestamptz,
    created_at                  timestamptz not null default now()
);

create index if not exists ix_scan_runs_batch_number on scan_runs(batch_id, run_number);
create index if not exists ix_scan_runs_status       on scan_runs(status);
create index if not exists ix_scan_runs_parent       on scan_runs(parent_run_id);


-- ---------------------------------------------------------------------------
-- INVOICE FILES  (one file record per uploaded file within a batch)
-- ---------------------------------------------------------------------------
create table if not exists invoice_files (
    id                bigserial   primary key,
    batch_id          uuid        not null references invoice_batches(id) on delete cascade,
    tenant_id         uuid        references tenants(id)   on delete set null,
    company_id        uuid        references companies(id) on delete set null,
    original_filename varchar(500) not null,
    stored_filename   varchar(500) not null,
    file_path         text         not null,
    mime_type         varchar(255),
    file_size_bytes   integer,
    status            varchar(50)  not null default 'uploaded',
    page_count        integer,
    error_message     text,
    uploaded_at       timestamptz  not null default now(),
    processed_at      timestamptz
);

-- Back-fill columns added after initial release
alter table invoice_files add column if not exists tenant_id       uuid references tenants(id)   on delete set null;
alter table invoice_files add column if not exists company_id      uuid references companies(id) on delete set null;
alter table invoice_files add column if not exists file_size_bytes integer;

create index if not exists idx_invoice_files_batch_id   on invoice_files(batch_id);
create index if not exists idx_invoice_files_tenant_id  on invoice_files(tenant_id);
create index if not exists idx_invoice_files_company_id on invoice_files(company_id);


-- ---------------------------------------------------------------------------
-- INVOICE ROWS  (one row per extracted invoice / line item)
-- ---------------------------------------------------------------------------
create table if not exists invoice_rows (
    id                       bigserial    primary key,
    batch_id                 uuid         not null references invoice_batches(id) on delete cascade,
    tenant_id                uuid         references tenants(id)       on delete set null,
    company_id               uuid         references companies(id)     on delete set null,
    scan_run_id              uuid         references scan_runs(id)     on delete set null,
    source_file_id           bigint       references invoice_files(id) on delete set null,
    source_filename          varchar(500),
    page_no                  integer      not null,
    supplier_name            text,
    supplier_posting_account varchar(100),
    nominal_account_code     varchar(100),
    invoice_number           text,
    invoice_date             date,
    description              text,
    line_items_raw           text,
    net_amount               numeric(14,2),
    vat_amount               numeric(14,2),
    total_amount             numeric(14,2),
    currency                 varchar(20),
    tax_code                 varchar(50),
    method_used              varchar(200),
    confidence_score         numeric(5,2),
    validation_status        varchar(100),
    review_required          boolean      not null default false,
    row_status               varchar(40)  not null default 'active',
    row_status_reason        varchar(80),
    row_status_note          text,
    row_status_changed_at    timestamptz,
    row_status_changed_by    uuid         references users(id) on delete set null,
    header_raw               text,
    totals_raw               text,
    page_text_raw            text,
    created_at               timestamptz  not null default now()
);

-- Back-fill columns added after initial release
alter table invoice_rows add column if not exists tenant_id               uuid         references tenants(id)       on delete set null;
alter table invoice_rows add column if not exists company_id              uuid         references companies(id)     on delete set null;
alter table invoice_rows add column if not exists scan_run_id             uuid         references scan_runs(id)     on delete set null;
alter table invoice_rows add column if not exists supplier_posting_account varchar(100);
alter table invoice_rows add column if not exists nominal_account_code    varchar(100);
alter table invoice_rows add column if not exists row_status              varchar(40)  not null default 'active';
alter table invoice_rows add column if not exists row_status_reason       varchar(80);
alter table invoice_rows add column if not exists row_status_note         text;
alter table invoice_rows add column if not exists row_status_changed_at   timestamptz;
alter table invoice_rows add column if not exists row_status_changed_by   uuid references users(id) on delete set null;
update invoice_rows set row_status = 'active' where row_status is null or row_status = '';

-- Widen method_used if it was created as varchar(50) on older installs
alter table invoice_rows alter column method_used type varchar(200);

create index if not exists idx_invoice_rows_batch_id    on invoice_rows(batch_id);
create index if not exists idx_invoice_rows_tenant_id   on invoice_rows(tenant_id);
create index if not exists idx_invoice_rows_company_id  on invoice_rows(company_id);
create index if not exists ix_invoice_rows_scan_run     on invoice_rows(scan_run_id);
create index if not exists ix_invoice_rows_export_status on invoice_rows(batch_id, scan_run_id, row_status);


-- ---------------------------------------------------------------------------
-- SUPPLIER PATTERNS  (keyword fingerprints for auto-matching future invoices)
-- ---------------------------------------------------------------------------
create table if not exists supplier_patterns (
    id          bigserial   primary key,
    tenant_id   uuid        not null references tenants(id)          on delete cascade,
    company_id  uuid        not null references companies(id)        on delete cascade,
    supplier_id bigint      not null references tenant_suppliers(id) on delete cascade,
    keywords    text,
    hit_count   integer     not null default 1,
    last_seen_at timestamptz not null default now(),
    created_at  timestamptz not null default now(),
    status      varchar(30) not null default 'active',
    trusted_outcome_source varchar(40),
    source_batch_id uuid,
    source_row_id bigint,
    created_by uuid references users(id) on delete set null,
    activated_at timestamptz,
    activated_by uuid references users(id) on delete set null,
    last_trusted_at timestamptz,
    proposed_keywords text,
    proposal_count integer not null default 0,
    last_proposed_at timestamptz,
    constraint uq_supplier_pattern unique (tenant_id, company_id, supplier_id)
);

create index if not exists idx_supplier_patterns_tenant_id   on supplier_patterns(tenant_id);
create index if not exists idx_supplier_patterns_supplier_id on supplier_patterns(supplier_id);
create index if not exists ix_supplier_patterns_tenant_company_status on supplier_patterns(tenant_id, company_id, status);
create index if not exists ix_supplier_patterns_supplier_status on supplier_patterns(tenant_id, company_id, supplier_id, status);


-- ---------------------------------------------------------------------------
-- INVOICE READ HEADERS  (one read snapshot per scanned page)
-- ---------------------------------------------------------------------------
create table if not exists invoice_read_headers (
    id                            bigserial    primary key,
    batch_id                      uuid         not null references invoice_batches(id) on delete cascade,
    tenant_id                     uuid         references tenants(id) on delete set null,
    company_id                    uuid         references companies(id) on delete set null,
    scan_run_id                   uuid         references scan_runs(id) on delete set null,
    source_file_id                bigint       references invoice_files(id) on delete set null,
    row_id                        bigint       references invoice_rows(id) on delete set null,
    source_filename               varchar(500),
    page_no                       integer      not null,
    provider_name                 varchar(80)  not null,
    extraction_source             varchar(80),
    method_used                   text,
    baseline_mode                 boolean      not null default false,
    document_type                 varchar(80),
    document_confidence           numeric(6,4),
    supplier_name                 text,
    supplier_vat                  varchar(100),
    supplier_address              text,
    supplier_address_recipient    text,
    customer_name                 text,
    customer_vat                  varchar(100),
    customer_address              text,
    customer_address_recipient    text,
    invoice_number                text,
    invoice_date                  varchar(80),
    due_date                      varchar(80),
    order_number                  varchar(120),
    purchase_order                varchar(120),
    description                   text,
    net_amount                    numeric(14,2),
    vat_amount                    numeric(14,2),
    total_amount                  numeric(14,2),
    currency                      varchar(20),
    header_text                   text,
    totals_text                   text,
    page_text                     text,
    raw_provider_fields           json,
    raw_provider_payload          json,
    raw_di_fields                 json,
    raw_di_payload                json,
    "BatchPages"                  integer,
    "DocumentInBatch"             integer,
    "DocType"                     varchar(80),
    "DocumentConfidence"          numeric(6,4),
    "CustomerName"                text,
    "CustomerId"                  varchar(120),
    "PurchaseOrder"               varchar(120),
    "InvoiceId"                   text,
    "InvoiceDate"                 varchar(80),
    "DueDate"                     varchar(80),
    "VendorName"                  text,
    "VendorAddress"               text,
    "VendorAddressRecipient"      text,
    "CustomerAddress"             text,
    "CustomerAddressRecipient"    text,
    "BillingAddress"              text,
    "BillingAddressRecipient"     text,
    "ShippingAddress"             text,
    "ShippingAddressRecipient"    text,
    "SubTotal"                    text,
    "TotalDiscount"               text,
    "TotalTax"                    text,
    "InvoiceTotal"                text,
    "AmountDue"                   text,
    "PreviousUnpaidBalance"       text,
    "RemittanceAddress"           text,
    "RemittanceAddressRecipient"  text,
    "ServiceAddress"              text,
    "ServiceAddressRecipient"     text,
    "ServiceStartDate"            varchar(80),
    "ServiceEndDate"              varchar(80),
    "VendorTaxId"                 varchar(120),
    "CustomerTaxId"               varchar(120),
    "PaymentTerm"                 text,
    "KVKNumber"                   varchar(120),
    "CurrencyCode"                varchar(20),
    "VendorPhoneNumber"           varchar(120),
    "CustomerPhoneNumber"         varchar(120),
    "BillingPhoneNumber"          varchar(120),
    "VendorEmail"                 varchar(255),
    "VendorFaxNumber"             varchar(120),
    "ReferenceNumber"             varchar(120),
    "PaymentDetails"              json,
    "TaxDetails"                  json,
    "PaidInFourInstallements"     json,
    created_at                    timestamptz  not null default now()
);

create index if not exists ix_invoice_read_headers_batch_page on invoice_read_headers(batch_id, page_no);
create index if not exists ix_invoice_read_headers_row        on invoice_read_headers(row_id);
create index if not exists ix_invoice_read_headers_provider   on invoice_read_headers(provider_name);
create index if not exists ix_invoice_read_headers_scan_run   on invoice_read_headers(scan_run_id);


-- ---------------------------------------------------------------------------
-- INVOICE READ DETAILS  (structured line items for each read snapshot)
-- ---------------------------------------------------------------------------
create table if not exists invoice_read_details (
    id           bigserial   primary key,
    header_id    bigint      not null references invoice_read_headers(id) on delete cascade,
    line_no      integer,
    description  text,
    quantity     numeric(14,4),
    unit_price   numeric(14,4),
    net_amount   numeric(14,2),
    tax_amount   numeric(14,2),
    di_amount       text,
    di_date         varchar(80),
    di_description  text,
    di_product_code varchar(120),
    di_quantity     varchar(80),
    di_tax          text,
    di_tax_rate     varchar(80),
    di_unit         varchar(80),
    di_unit_price   text,
    raw_detail   json,
    created_at   timestamptz not null default now()
);

create index if not exists ix_invoice_read_details_header on invoice_read_details(header_id);


-- ---------------------------------------------------------------------------
-- INVOICE FIELD CANDIDATES  (field evidence for resolver/review/learning)
-- ---------------------------------------------------------------------------
create table if not exists invoice_field_candidates (
    id               bigserial   primary key,
    tenant_id        uuid        not null references tenants(id) on delete cascade,
    company_id       uuid        references companies(id) on delete set null,
    batch_id         uuid        not null references invoice_batches(id) on delete cascade,
    scan_run_id      uuid        references scan_runs(id) on delete set null,
    row_id           bigint      not null references invoice_rows(id) on delete cascade,
    source_file_id   bigint      references invoice_files(id) on delete set null,
    field_name       varchar(80) not null,
    candidate_value  text,
    normalised_value text,
    source_type      varchar(80) not null,
    source_id        text,
    confidence       numeric(6,4),
    evidence         text,
    reason           text,
    selected         boolean     not null default false,
    applied          boolean     not null default false,
    rejected_reason  text,
    conflict         boolean     not null default false,
    user_accepted    boolean     not null default false,
    user_corrected   boolean     not null default false,
    final_value      text,
    finalised_at     timestamptz,
    finalised_by     uuid references users(id),
    outcome_source   varchar(40),
    created_at       timestamptz not null default now()
);

create index if not exists ix_field_candidates_tenant_company on invoice_field_candidates(tenant_id, company_id);
create index if not exists ix_field_candidates_batch_row     on invoice_field_candidates(batch_id, row_id);
create index if not exists ix_field_candidates_field_name    on invoice_field_candidates(field_name);
create index if not exists ix_field_candidates_source_type   on invoice_field_candidates(source_type);
create index if not exists ix_field_candidates_selected      on invoice_field_candidates(selected);
create index if not exists ix_field_candidates_created_at    on invoice_field_candidates(created_at);
create index if not exists ix_field_candidates_scan_run      on invoice_field_candidates(scan_run_id);


-- ---------------------------------------------------------------------------
-- CROSS-BATCH DUPLICATE CANDIDATES
-- ---------------------------------------------------------------------------
create table if not exists invoice_duplicate_candidates (
    id                        serial      primary key,
    tenant_id                 uuid        not null references tenants(id) on delete cascade,
    company_id                uuid        references companies(id) on delete set null,
    batch_id                  uuid        not null references invoice_batches(id) on delete cascade,
    scan_run_id               uuid        references scan_runs(id) on delete set null,
    row_id                    bigint      not null references invoice_rows(id) on delete cascade,
    candidate_batch_id        uuid        not null references invoice_batches(id) on delete cascade,
    candidate_scan_run_id     uuid        references scan_runs(id) on delete set null,
    candidate_row_id          bigint      not null references invoice_rows(id) on delete cascade,
    match_type                varchar(40) not null default 'cross_batch',
    match_status              varchar(40) not null,
    confidence                numeric(6,4),
    evidence_json             text,
    normalized_invoice_number varchar(160),
    document_type             varchar(80),
    supplier_key              varchar(255),
    supplier_vat              varchar(100),
    invoice_date              date,
    total_cents               bigint,
    currency                  varchar(20),
    document_fingerprint      varchar(80),
    created_at                timestamptz not null default now(),
    resolved_at               timestamptz,
    resolved_by               uuid        references users(id)
);

create index if not exists ix_duplicate_candidates_tenant_company on invoice_duplicate_candidates(tenant_id, company_id);
create index if not exists ix_duplicate_candidates_batch_row      on invoice_duplicate_candidates(batch_id, row_id);
create index if not exists ix_duplicate_candidates_candidate_row  on invoice_duplicate_candidates(candidate_batch_id, candidate_row_id);
create index if not exists ix_duplicate_candidates_scan_run       on invoice_duplicate_candidates(scan_run_id);
create index if not exists ix_duplicate_candidates_status         on invoice_duplicate_candidates(match_status);
create unique index if not exists uq_duplicate_candidates_pair_type on invoice_duplicate_candidates(row_id, candidate_row_id, match_type);


-- =============================================================================
-- End of schema
-- =============================================================================
