-- =============================================================================
-- ApprovLinq one-time rebuild for invoice read snapshot tables
-- =============================================================================
-- Use this only for environments affected by the failed release where the two
-- new read snapshot tables were created with the wrong shape.
--
-- This drops ONLY invoice_read_details and invoice_read_headers, then recreates
-- them with the current application schema. These tables are read snapshots
-- generated from invoice processing; dropping them removes any saved snapshot
-- rows already captured in these two tables.
-- =============================================================================

begin;

drop table if exists invoice_read_details cascade;
drop table if exists invoice_read_headers cascade;

create table invoice_read_headers (
    id                            bigserial    primary key,
    batch_id                      uuid         not null references invoice_batches(id) on delete cascade,
    tenant_id                     uuid         references tenants(id) on delete set null,
    company_id                    uuid         references companies(id) on delete set null,
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

create index ix_invoice_read_headers_batch_page on invoice_read_headers(batch_id, page_no);
create index ix_invoice_read_headers_row        on invoice_read_headers(row_id);
create index ix_invoice_read_headers_provider   on invoice_read_headers(provider_name);

create table invoice_read_details (
    id             bigserial   primary key,
    header_id      bigint      not null references invoice_read_headers(id) on delete cascade,
    line_no        integer,
    description    text,
    quantity       numeric(14,4),
    unit_price     numeric(14,4),
    net_amount     numeric(14,2),
    tax_amount     numeric(14,2),
    di_amount       text,
    di_date         varchar(80),
    di_description  text,
    di_product_code varchar(120),
    di_quantity     varchar(80),
    di_tax          text,
    di_tax_rate     varchar(80),
    di_unit         varchar(80),
    di_unit_price   text,
    raw_detail     json,
    created_at     timestamptz not null default now()
);

create index ix_invoice_read_details_header on invoice_read_details(header_id);

commit;
