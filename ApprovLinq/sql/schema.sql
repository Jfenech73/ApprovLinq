-- =============================================================================
-- ApprovLinq Invoice Scanner Service — Complete Database Schema
-- Safe to run on a fresh database OR an existing one (all statements are
-- idempotent: CREATE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, etc.)
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
    id                   bigserial    primary key,
    tenant_id            uuid         not null references tenants(id)   on delete cascade,
    company_id           uuid         not null references companies(id) on delete cascade,
    supplier_account_code varchar(100),
    supplier_name        varchar(255) not null,
    default_nominal      varchar(100),
    posting_account      varchar(100) not null,
    is_active            boolean      not null default true,
    created_at           timestamptz  not null default now(),
    updated_at           timestamptz  not null default now(),
    constraint uq_tenant_company_supplier_name
        unique (tenant_id, company_id, supplier_name),
    constraint uq_tenant_company_supplier_account_code
        unique (tenant_id, company_id, supplier_account_code)
);

-- Back-fill columns added after initial release (safe on existing DBs)
alter table tenant_suppliers add column if not exists company_id           uuid references companies(id) on delete cascade;
alter table tenant_suppliers add column if not exists supplier_account_code varchar(100);
alter table tenant_suppliers add column if not exists default_nominal       varchar(100);

-- Partial unique index on account code (NULL rows excluded)
create unique index if not exists ix_tenant_suppliers_tenant_company_account_code
    on tenant_suppliers(tenant_id, company_id, supplier_account_code)
    where supplier_account_code is not null;

-- Legacy index without company_id (kept for backwards compat with old rows)
create unique index if not exists ix_tenant_suppliers_tenant_account_code
    on tenant_suppliers(tenant_id, supplier_account_code)
    where supplier_account_code is not null and company_id is null;


-- ---------------------------------------------------------------------------
-- TENANT NOMINAL ACCOUNTS  (chart of accounts per tenant + company)
-- ---------------------------------------------------------------------------
create table if not exists tenant_nominal_accounts (
    id           bigserial    primary key,
    tenant_id    uuid         not null references tenants(id)   on delete cascade,
    company_id   uuid         not null references companies(id) on delete cascade,
    account_code varchar(100) not null,
    account_name varchar(255) not null,
    is_active    boolean      not null default true,
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now(),
    constraint uq_tenant_company_nominal_account_code
        unique (tenant_id, company_id, account_code)
);

-- Back-fill column added after initial release
alter table tenant_nominal_accounts add column if not exists company_id uuid references companies(id) on delete cascade;


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
    created_at      timestamptz  not null default now(),
    processed_at    timestamptz
);

-- Back-fill columns added after initial release
alter table invoice_batches add column if not exists tenant_id    uuid references tenants(id)   on delete set null;
alter table invoice_batches add column if not exists company_id   uuid references companies(id) on delete set null;
alter table invoice_batches add column if not exists scan_mode    varchar(20) default 'summary';

-- Ensure scan_mode is never NULL on old rows
update invoice_batches set scan_mode = 'summary' where scan_mode is null;

-- Foreign key constraints (idempotent guard)
do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'fk_invoice_batches_tenant') then
        alter table invoice_batches
            add constraint fk_invoice_batches_tenant
            foreign key (tenant_id) references tenants(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'fk_invoice_batches_company') then
        alter table invoice_batches
            add constraint fk_invoice_batches_company
            foreign key (company_id) references companies(id) on delete set null;
    end if;
end $$;

create index if not exists idx_invoice_batches_tenant_id  on invoice_batches(tenant_id);
create index if not exists idx_invoice_batches_company_id on invoice_batches(company_id);


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

-- Foreign key constraints (idempotent guard)
do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'fk_invoice_files_tenant') then
        alter table invoice_files
            add constraint fk_invoice_files_tenant
            foreign key (tenant_id) references tenants(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'fk_invoice_files_company') then
        alter table invoice_files
            add constraint fk_invoice_files_company
            foreign key (company_id) references companies(id) on delete set null;
    end if;
end $$;

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
    source_file_id           integer      references invoice_files(id) on delete set null,
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
    header_raw               text,
    totals_raw               text,
    page_text_raw            text,
    created_at               timestamptz  not null default now()
);

-- Back-fill columns added after initial release
alter table invoice_rows add column if not exists tenant_id               uuid         references tenants(id)       on delete set null;
alter table invoice_rows add column if not exists company_id              uuid         references companies(id)     on delete set null;
alter table invoice_rows add column if not exists supplier_posting_account varchar(100);
alter table invoice_rows add column if not exists nominal_account_code    varchar(100);

-- Widen method_used if it was created as varchar(50) on older installs
alter table invoice_rows alter column method_used type varchar(200);

-- Foreign key constraints (idempotent guard)
do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'fk_invoice_rows_tenant') then
        alter table invoice_rows
            add constraint fk_invoice_rows_tenant
            foreign key (tenant_id) references tenants(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'fk_invoice_rows_company') then
        alter table invoice_rows
            add constraint fk_invoice_rows_company
            foreign key (company_id) references companies(id) on delete set null;
    end if;
end $$;

-- Back-fill tenant_id from the batch's company where not set
update invoice_rows as r
set    tenant_id = c.tenant_id
from   companies as c
where  r.company_id = c.id
  and  r.tenant_id is null;

create index if not exists idx_invoice_rows_batch_id    on invoice_rows(batch_id);
create index if not exists idx_invoice_rows_tenant_id   on invoice_rows(tenant_id);
create index if not exists idx_invoice_rows_company_id  on invoice_rows(company_id);


-- ---------------------------------------------------------------------------
-- SUPPLIER PATTERNS  (keyword fingerprints for auto-matching future invoices)
-- ---------------------------------------------------------------------------
create table if not exists supplier_patterns (
    id          bigserial   primary key,
    tenant_id   uuid        not null references tenants(id)          on delete cascade,
    company_id  uuid        not null references companies(id)        on delete cascade,
    supplier_id integer     not null references tenant_suppliers(id) on delete cascade,
    keywords    text,
    hit_count   integer     not null default 1,
    last_seen_at timestamptz not null default now(),
    created_at  timestamptz not null default now(),
    constraint uq_supplier_pattern unique (tenant_id, company_id, supplier_id)
);

create index if not exists idx_supplier_patterns_tenant_id   on supplier_patterns(tenant_id);
create index if not exists idx_supplier_patterns_supplier_id on supplier_patterns(supplier_id);


-- =============================================================================
-- End of schema
-- =============================================================================
