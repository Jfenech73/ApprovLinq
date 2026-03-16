create extension if not exists pgcrypto;

create table if not exists tenants (
    id uuid primary key default gen_random_uuid(),
    tenant_code text unique not null,
    tenant_name text not null,
    status text not null default 'active',
    is_active boolean not null default true,
    contact_name text,
    contact_email text,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    email text unique not null,
    full_name text not null,
    password_hash text not null,
    role text not null default 'tenant',
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists user_tenants (
    id bigserial primary key,
    user_id uuid not null references users(id) on delete cascade,
    tenant_id uuid not null references tenants(id) on delete cascade,
    tenant_role text not null default 'tenant_admin',
    is_default boolean not null default false,
    created_at timestamptz not null default now(),
    constraint uq_user_tenant unique (user_id, tenant_id)
);

create table if not exists user_sessions (
    id bigserial primary key,
    user_id uuid not null references users(id) on delete cascade,
    token_hash text unique not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    revoked_at timestamptz
);

create table if not exists companies (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id) on delete cascade,
    company_code text not null,
    company_name text not null,
    registration_number text,
    vat_number text,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint uq_tenant_company_code unique (tenant_id, company_code)
);

create table if not exists tenant_suppliers (
    id bigserial primary key,
    tenant_id uuid not null references tenants(id) on delete cascade,
    company_id uuid references companies(id) on delete cascade,
    supplier_account_code text,
    supplier_name text not null,
    default_nominal text,
    posting_account text not null,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists tenant_nominal_accounts (
    id bigserial primary key,
    tenant_id uuid not null references tenants(id) on delete cascade,
    account_code text not null,
    account_name text not null,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint uq_tenant_nominal_account_code unique (tenant_id, account_code)
);

create table if not exists issue_logs (
    id bigserial primary key,
    tenant_id uuid not null references tenants(id) on delete cascade,
    created_by_user_id uuid references users(id) on delete set null,
    title text not null,
    description text not null,
    status text not null default 'pending',
    priority text not null default 'normal',
    resolution_notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table invoice_batches
    add column if not exists tenant_id uuid,
    add column if not exists company_id uuid;

alter table invoice_files
    add column if not exists tenant_id uuid,
    add column if not exists company_id uuid,
    add column if not exists file_size_bytes int;

alter table invoice_rows
    add column if not exists tenant_id uuid,
    add column if not exists company_id uuid,
    add column if not exists supplier_posting_account text,
    add column if not exists nominal_account_code text;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'fk_invoice_batches_tenant'
    ) then
        alter table invoice_batches
            add constraint fk_invoice_batches_tenant
            foreign key (tenant_id) references tenants(id) on delete set null;
    end if;

    if not exists (
        select 1 from pg_constraint where conname = 'fk_invoice_batches_company'
    ) then
        alter table invoice_batches
            add constraint fk_invoice_batches_company
            foreign key (company_id) references companies(id) on delete set null;
    end if;

    if not exists (
        select 1 from pg_constraint where conname = 'fk_invoice_files_tenant'
    ) then
        alter table invoice_files
            add constraint fk_invoice_files_tenant
            foreign key (tenant_id) references tenants(id) on delete set null;
    end if;

    if not exists (
        select 1 from pg_constraint where conname = 'fk_invoice_files_company'
    ) then
        alter table invoice_files
            add constraint fk_invoice_files_company
            foreign key (company_id) references companies(id) on delete set null;
    end if;

    if not exists (
        select 1 from pg_constraint where conname = 'fk_invoice_rows_tenant'
    ) then
        alter table invoice_rows
            add constraint fk_invoice_rows_tenant
            foreign key (tenant_id) references tenants(id) on delete set null;
    end if;

    if not exists (
        select 1 from pg_constraint where conname = 'fk_invoice_rows_company'
    ) then
        alter table invoice_rows
            add constraint fk_invoice_rows_company
            foreign key (company_id) references companies(id) on delete set null;
    end if;
end $$;

update invoice_batches b
set tenant_id = c.tenant_id
from companies c
where b.company_id = c.id
  and b.tenant_id is null;

update invoice_files f
set tenant_id = c.tenant_id
from companies c
where f.company_id = c.id
  and f.tenant_id is null;

update invoice_rows r
set tenant_id = c.tenant_id
from companies c
where r.company_id = c.id
  and r.tenant_id is null;

create index if not exists idx_invoice_batches_tenant_id on invoice_batches(tenant_id);
create index if not exists idx_invoice_batches_company_id on invoice_batches(company_id);
create index if not exists idx_invoice_files_tenant_id on invoice_files(tenant_id);
create index if not exists idx_invoice_files_company_id on invoice_files(company_id);
create index if not exists idx_invoice_rows_tenant_id on invoice_rows(tenant_id);
create index if not exists idx_invoice_rows_company_id on invoice_rows(company_id);

-- Only after the app is confirmed to write tenant_id consistently:
-- alter table invoice_batches alter column tenant_id set not null;
-- alter table invoice_files alter column tenant_id set not null;
-- alter table invoice_rows alter column tenant_id set not null;

alter table tenant_suppliers add column if not exists supplier_account_code text;
alter table tenant_suppliers add column if not exists default_nominal text;

update tenant_suppliers
set supplier_account_code = coalesce(nullif(supplier_account_code, ''), posting_account)
where supplier_account_code is null or supplier_account_code = '';

alter table tenant_suppliers add column if not exists company_id uuid references companies(id) on delete cascade;
alter table tenant_nominal_accounts add column if not exists company_id uuid references companies(id) on delete cascade;

update tenant_suppliers ts
set company_id = c.id
from companies c
where ts.company_id is null
  and c.tenant_id = ts.tenant_id;

update tenant_nominal_accounts na
set company_id = c.id
from companies c
where na.company_id is null
  and c.tenant_id = na.tenant_id;

do $$
begin
    begin
        alter table tenant_suppliers drop constraint uq_tenant_supplier_name;
    exception when undefined_object then null;
    end;
    begin
        alter table tenant_suppliers drop constraint uq_tenant_company_supplier_name;
    exception when undefined_object then null;
    end;
end $$;

drop index if exists ix_tenant_suppliers_tenant_account_code;
drop index if exists ix_tenant_suppliers_tenant_company_supplier_name;

create unique index if not exists ix_tenant_suppliers_tenant_company_account_code
    on tenant_suppliers(tenant_id, company_id, supplier_account_code)
    where supplier_account_code is not null;

create index if not exists ix_tenant_suppliers_tenant_company_name
    on tenant_suppliers(tenant_id, company_id, supplier_name);
