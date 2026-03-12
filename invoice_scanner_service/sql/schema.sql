create extension if not exists pgcrypto;

create table if not exists invoice_batches (
    id uuid primary key default gen_random_uuid(),
    batch_name text not null,
    source_filename text,
    status text not null default 'created',
    page_count int,
    notes text,
    created_at timestamptz not null default now(),
    processed_at timestamptz
);

create table if not exists invoice_rows (
    id bigserial primary key,
    batch_id uuid not null references invoice_batches(id) on delete cascade,
    page_no int not null,
    supplier_name text,
    invoice_number text,
    invoice_date date,
    description text,
    net_amount numeric(14,2),
    vat_amount numeric(14,2),
    total_amount numeric(14,2),
    currency text,
    tax_code text,
    method_used text,
    confidence_score numeric(5,2),
    validation_status text,
    review_required boolean not null default false,
    header_raw text,
    totals_raw text,
    page_text_raw text,
    created_at timestamptz not null default now()
);

create index if not exists idx_invoice_rows_batch_id on invoice_rows(batch_id);
create index if not exists idx_invoice_rows_invoice_number on invoice_rows(invoice_number);
create index if not exists idx_invoice_rows_invoice_date on invoice_rows(invoice_date);
