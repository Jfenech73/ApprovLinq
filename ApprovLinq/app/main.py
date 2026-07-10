import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.db import models
from app.db.session import engine
from app.routers import analytics, auth, admin, admin_export_templates, batches, health, tenant

logger = logging.getLogger(__name__)

try:
    models.Base.metadata.create_all(bind=engine)
except Exception as exc:
    logger.warning("create_all failed (non-fatal): %s", exc)


def ensure_runtime_schema() -> None:
    """Apply incremental schema migrations at startup.

    Each statement runs in its OWN connection and transaction so that one
    failure (e.g. column already exists) never aborts the remaining
    statements.  All statements are idempotent (IF NOT EXISTS / IF EXISTS).
    """
    if engine.dialect.name != "postgresql":
        return

    statements: list[str] = [
        # ── tenants ──────────────────────────────────────────────────────────
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) NOT NULL DEFAULT 'summary'",

        # ── tenant_suppliers ─────────────────────────────────────────────────
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS supplier_account_code VARCHAR(100)",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS default_nominal VARCHAR(100)",
        # Back-fill supplier_account_code from posting_account where blank
        "UPDATE tenant_suppliers SET supplier_account_code = COALESCE(NULLIF(supplier_account_code, ''), posting_account) WHERE supplier_account_code IS NULL OR supplier_account_code = ''",
        # Back-fill company_id from the first company in the same tenant
        "UPDATE tenant_suppliers AS ts SET company_id = c.id FROM companies AS c WHERE ts.company_id IS NULL AND c.tenant_id = ts.tenant_id",
        "CREATE INDEX IF NOT EXISTS ix_tenant_suppliers_tenant_company_account_code ON tenant_suppliers (tenant_id, company_id, supplier_account_code)",

        # ── tenant_nominal_accounts ──────────────────────────────────────────
        "ALTER TABLE tenant_nominal_accounts ADD COLUMN IF NOT EXISTS company_id UUID",
        "UPDATE tenant_nominal_accounts AS na SET company_id = c.id FROM companies AS c WHERE na.company_id IS NULL AND c.tenant_id = na.tenant_id",
        "CREATE INDEX IF NOT EXISTS ix_tenant_nominals_tenant_company_account_code ON tenant_nominal_accounts (tenant_id, company_id, account_code)",
        "ALTER TABLE tenant_nominal_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE",

        # ── invoice_batches ──────────────────────────────────────────────────
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) DEFAULT 'summary'",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS current_scan_run_id UUID",
        "UPDATE invoice_batches SET scan_mode = COALESCE(NULLIF(scan_mode, ''), 'summary')",
        """CREATE TABLE IF NOT EXISTS scan_runs (
            id UUID PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
            company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
            run_number INTEGER NOT NULL,
            parent_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            status VARCHAR(40) NOT NULL DEFAULT 'processing',
            app_version VARCHAR(80),
            extractor_build_tag VARCHAR(120),
            scan_mode VARCHAR(20),
            settings_fingerprint VARCHAR(64),
            provider_config_fingerprint VARCHAR(64),
            selected_backend VARCHAR(80),
            page_count INTEGER,
            row_count INTEGER,
            notes TEXT,
            error_message TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_scan_runs_batch_number ON scan_runs(batch_id, run_number)",
        "CREATE INDEX IF NOT EXISTS ix_scan_runs_status ON scan_runs(status)",
        "CREATE INDEX IF NOT EXISTS ix_scan_runs_parent ON scan_runs(parent_run_id)",
        """INSERT INTO scan_runs (
            id, batch_id, tenant_id, company_id, run_number, status, scan_mode,
            page_count, row_count, notes, started_at, completed_at, created_at
        )
        SELECT
            (
                substr(md5(b.id::text), 1, 8) || '-' ||
                substr(md5(b.id::text), 9, 4) || '-' ||
                substr(md5(b.id::text), 13, 4) || '-' ||
                substr(md5(b.id::text), 17, 4) || '-' ||
                substr(md5(b.id::text), 21, 12)
            )::uuid,
            b.id, b.tenant_id, b.company_id, 1, COALESCE(NULLIF(b.status, ''), 'backfilled'),
            b.scan_mode, b.page_count,
            (SELECT COUNT(*) FROM invoice_rows r WHERE r.batch_id = b.id),
            b.notes, COALESCE(b.created_at, NOW()), b.processed_at, COALESCE(b.created_at, NOW())
        FROM invoice_batches b
        WHERE b.current_scan_run_id IS NULL
        ON CONFLICT (id) DO NOTHING""",
        """UPDATE invoice_batches b
        SET current_scan_run_id = sr.id
        FROM scan_runs sr
        WHERE b.current_scan_run_id IS NULL
          AND sr.batch_id = b.id
          AND sr.run_number = 1""",

        # ── invoice_files ────────────────────────────────────────────────────
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS file_bytes BYTEA",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(30) NOT NULL DEFAULT 'database+local'",
        "UPDATE invoice_files AS f SET company_id = b.company_id FROM invoice_batches AS b WHERE f.company_id IS NULL AND b.id = f.batch_id",

        # ── invoice_rows ─────────────────────────────────────────────────────
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS supplier_posting_account VARCHAR(100)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS nominal_account_code VARCHAR(100)",
        "ALTER TABLE invoice_rows ALTER COLUMN method_used TYPE TEXT",
        "ALTER TABLE invoice_rows ALTER COLUMN review_reasons TYPE TEXT",
        "ALTER TABLE invoice_rows ALTER COLUMN review_fields TYPE TEXT",
        "UPDATE invoice_rows AS r SET company_id = b.company_id FROM invoice_batches AS b WHERE r.company_id IS NULL AND b.id = r.batch_id",

        # ── tenant_suppliers — new columns ───────────────────────────────────
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS vat_number VARCHAR(100)",

        # ── invoice_rows — new columns ────────────────────────────────────────
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS review_reasons VARCHAR(500)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS review_priority VARCHAR(20)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS review_fields VARCHAR(300)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS row_status VARCHAR(40) NOT NULL DEFAULT 'active'",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS row_status_reason VARCHAR(80)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS row_status_note TEXT",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS row_status_changed_at TIMESTAMPTZ",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS row_status_changed_by UUID REFERENCES users(id) ON DELETE SET NULL",
        "UPDATE invoice_rows SET row_status = 'active' WHERE row_status IS NULL OR row_status = ''",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS page_quality_score NUMERIC(4,2)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS classification_method VARCHAR(50)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS supplier_match_method VARCHAR(50)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS totals_reconciliation_status VARCHAR(50)",
        "CREATE INDEX IF NOT EXISTS ix_invoice_rows_scan_run ON invoice_rows(scan_run_id)",
        "CREATE INDEX IF NOT EXISTS ix_invoice_rows_export_status ON invoice_rows(batch_id, scan_run_id, row_status)",
        """UPDATE invoice_rows r
        SET scan_run_id = b.current_scan_run_id
        FROM invoice_batches b
        WHERE r.scan_run_id IS NULL AND r.batch_id = b.id""",

        # Koyeb-safe read snapshot tables. These must be created/extended
        # idempotently on restart; never rebuild them with DROP TABLE from an
        # app startup or deploy command.
        """CREATE TABLE IF NOT EXISTS invoice_read_headers (
            id SERIAL PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
            company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
            scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            source_file_id BIGINT REFERENCES invoice_files(id) ON DELETE SET NULL,
            row_id BIGINT REFERENCES invoice_rows(id) ON DELETE SET NULL,
            source_filename VARCHAR(500),
            page_no INTEGER NOT NULL,
            provider_name VARCHAR(80) NOT NULL,
            extraction_source VARCHAR(80),
            method_used TEXT,
            baseline_mode BOOLEAN NOT NULL DEFAULT FALSE,
            document_type VARCHAR(80),
            document_confidence NUMERIC(6,4),
            supplier_name TEXT,
            supplier_vat VARCHAR(100),
            supplier_address TEXT,
            supplier_address_recipient TEXT,
            customer_name TEXT,
            customer_vat VARCHAR(100),
            customer_address TEXT,
            customer_address_recipient TEXT,
            invoice_number TEXT,
            invoice_date VARCHAR(80),
            due_date VARCHAR(80),
            order_number VARCHAR(120),
            purchase_order VARCHAR(120),
            description TEXT,
            net_amount NUMERIC(14,2),
            vat_amount NUMERIC(14,2),
            total_amount NUMERIC(14,2),
            currency VARCHAR(20),
            header_text TEXT,
            totals_text TEXT,
            page_text TEXT,
            raw_provider_fields JSON,
            raw_provider_payload JSON,
            raw_di_fields JSON,
            raw_di_payload JSON,
            "BatchPages" INTEGER,
            "DocumentInBatch" INTEGER,
            "DocType" VARCHAR(80),
            "DocumentConfidence" NUMERIC(6,4),
            "CustomerName" TEXT,
            "CustomerId" VARCHAR(120),
            "PurchaseOrder" VARCHAR(120),
            "InvoiceId" TEXT,
            "InvoiceDate" VARCHAR(80),
            "DueDate" VARCHAR(80),
            "VendorName" TEXT,
            "VendorAddress" TEXT,
            "VendorAddressRecipient" TEXT,
            "CustomerAddress" TEXT,
            "CustomerAddressRecipient" TEXT,
            "BillingAddress" TEXT,
            "BillingAddressRecipient" TEXT,
            "ShippingAddress" TEXT,
            "ShippingAddressRecipient" TEXT,
            "SubTotal" TEXT,
            "TotalDiscount" TEXT,
            "TotalTax" TEXT,
            "InvoiceTotal" TEXT,
            "AmountDue" TEXT,
            "PreviousUnpaidBalance" TEXT,
            "RemittanceAddress" TEXT,
            "RemittanceAddressRecipient" TEXT,
            "ServiceAddress" TEXT,
            "ServiceAddressRecipient" TEXT,
            "ServiceStartDate" VARCHAR(80),
            "ServiceEndDate" VARCHAR(80),
            "VendorTaxId" VARCHAR(120),
            "CustomerTaxId" VARCHAR(120),
            "PaymentTerm" TEXT,
            "KVKNumber" VARCHAR(120),
            "CurrencyCode" VARCHAR(20),
            "VendorPhoneNumber" VARCHAR(120),
            "CustomerPhoneNumber" VARCHAR(120),
            "BillingPhoneNumber" VARCHAR(120),
            "VendorEmail" VARCHAR(255),
            "VendorFaxNumber" VARCHAR(120),
            "ReferenceNumber" VARCHAR(120),
            "PaymentDetails" JSON,
            "TaxDetails" JSON,
            "PaidInFourInstallements" JSON,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS id BIGSERIAL",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS batch_id UUID REFERENCES invoice_batches(id) ON DELETE CASCADE",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS page_no INTEGER",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS provider_name VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "CREATE INDEX IF NOT EXISTS ix_invoice_read_headers_batch_page ON invoice_read_headers(batch_id, page_no)",
        "CREATE INDEX IF NOT EXISTS ix_invoice_read_headers_row ON invoice_read_headers(row_id)",
        "CREATE INDEX IF NOT EXISTS ix_invoice_read_headers_provider ON invoice_read_headers(provider_name)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies(id) ON DELETE SET NULL",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS source_file_id BIGINT REFERENCES invoice_files(id) ON DELETE SET NULL",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS row_id BIGINT REFERENCES invoice_rows(id) ON DELETE SET NULL",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS source_filename VARCHAR(500)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS extraction_source VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS method_used TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS baseline_mode BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS document_type VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS document_confidence NUMERIC(6,4)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS supplier_name TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS supplier_vat VARCHAR(100)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS supplier_address TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS supplier_address_recipient TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS customer_name TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS customer_vat VARCHAR(100)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS customer_address TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS customer_address_recipient TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS invoice_number TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS invoice_date VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS due_date VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS order_number VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS purchase_order VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS net_amount NUMERIC(14,2)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS vat_amount NUMERIC(14,2)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS total_amount NUMERIC(14,2)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS currency VARCHAR(20)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS header_text TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS totals_text TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS page_text TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS raw_provider_fields JSON",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS raw_provider_payload JSON",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS raw_di_fields JSON",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS raw_di_payload JSON",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"BatchPages\" INTEGER",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"DocumentInBatch\" INTEGER",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"DocType\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"DocumentConfidence\" NUMERIC(6,4)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CustomerName\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CustomerId\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"PurchaseOrder\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"InvoiceId\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"InvoiceDate\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"DueDate\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorName\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorAddress\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorAddressRecipient\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CustomerAddress\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CustomerAddressRecipient\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"BillingAddress\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"BillingAddressRecipient\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ShippingAddress\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ShippingAddressRecipient\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"SubTotal\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"TotalDiscount\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"TotalTax\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"InvoiceTotal\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"AmountDue\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"PreviousUnpaidBalance\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"RemittanceAddress\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"RemittanceAddressRecipient\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ServiceAddress\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ServiceAddressRecipient\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ServiceStartDate\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ServiceEndDate\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorTaxId\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CustomerTaxId\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"PaymentTerm\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"KVKNumber\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CurrencyCode\" VARCHAR(20)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorPhoneNumber\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"CustomerPhoneNumber\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"BillingPhoneNumber\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorEmail\" VARCHAR(255)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"VendorFaxNumber\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"ReferenceNumber\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"PaymentDetails\" JSON",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"TaxDetails\" JSON",
        "ALTER TABLE IF EXISTS invoice_read_headers ADD COLUMN IF NOT EXISTS \"PaidInFourInstallements\" JSON",
        "CREATE INDEX IF NOT EXISTS ix_invoice_read_headers_scan_run ON invoice_read_headers(scan_run_id)",
        """UPDATE invoice_read_headers h
        SET scan_run_id = b.current_scan_run_id
        FROM invoice_batches b
        WHERE h.scan_run_id IS NULL AND h.batch_id = b.id""",
        """CREATE TABLE IF NOT EXISTS invoice_read_details (
            id BIGSERIAL PRIMARY KEY,
            header_id BIGINT NOT NULL REFERENCES invoice_read_headers(id) ON DELETE CASCADE,
            line_no INTEGER,
            description TEXT,
            quantity NUMERIC(14,4),
            unit_price NUMERIC(14,4),
            net_amount NUMERIC(14,2),
            tax_amount NUMERIC(14,2),
            "Amount" TEXT,
            "Date" VARCHAR(80),
            "Description" TEXT,
            "ProductCode" VARCHAR(120),
            "Quantity" VARCHAR(80),
            "Tax" TEXT,
            "TaxRate" VARCHAR(80),
            "Unit" VARCHAR(80),
            "UnitPrice" TEXT,
            raw_detail JSON,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS id BIGSERIAL",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS header_id BIGINT REFERENCES invoice_read_headers(id) ON DELETE CASCADE",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "CREATE INDEX IF NOT EXISTS ix_invoice_read_details_header ON invoice_read_details(header_id)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS line_no INTEGER",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS quantity NUMERIC(14,4)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS unit_price NUMERIC(14,4)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS net_amount NUMERIC(14,2)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(14,2)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"Amount\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"Date\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"Description\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"ProductCode\" VARCHAR(120)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"Quantity\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"Tax\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"TaxRate\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"Unit\" VARCHAR(80)",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS \"UnitPrice\" TEXT",
        "ALTER TABLE IF EXISTS invoice_read_details ADD COLUMN IF NOT EXISTS raw_detail JSON",

        # ── supplier_patterns (new table) ────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS supplier_patterns ("
            "id SERIAL PRIMARY KEY,"
            "tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
            "company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,"
            "supplier_id INTEGER NOT NULL REFERENCES tenant_suppliers(id) ON DELETE CASCADE,"
            "keywords TEXT,"
            "hit_count INTEGER NOT NULL DEFAULT 1,"
            "last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "status VARCHAR(30) NOT NULL DEFAULT 'active',"
            "trusted_outcome_source VARCHAR(40),"
            "source_batch_id UUID,"
            "source_row_id BIGINT,"
            "created_by UUID REFERENCES users(id) ON DELETE SET NULL,"
            "activated_at TIMESTAMPTZ,"
            "activated_by UUID REFERENCES users(id) ON DELETE SET NULL,"
            "last_trusted_at TIMESTAMPTZ,"
            "proposed_keywords TEXT,"
            "proposal_count INTEGER NOT NULL DEFAULT 0,"
            "last_proposed_at TIMESTAMPTZ,"
            "CONSTRAINT uq_supplier_pattern UNIQUE (tenant_id, company_id, supplier_id)"
            ")"
        ),
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'active'",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS trusted_outcome_source VARCHAR(40)",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS source_batch_id UUID",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS source_row_id BIGINT",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS activated_by UUID REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS last_trusted_at TIMESTAMPTZ",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS proposed_keywords TEXT",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS proposal_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS last_proposed_at TIMESTAMPTZ",
        "UPDATE supplier_patterns SET status = 'active' WHERE status IS NULL OR status = ''",
        "CREATE INDEX IF NOT EXISTS ix_supplier_patterns_tenant_company_status ON supplier_patterns(tenant_id, company_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_supplier_patterns_supplier_status ON supplier_patterns(tenant_id, company_id, supplier_id, status)",

        # >>> REVIEW_PACK startup_alters
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS exported_at TIMESTAMPTZ",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS exported_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS reopened_at TIMESTAMPTZ",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS reopened_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS current_export_version INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS invoice_row_corrections (
            row_id BIGINT PRIMARY KEY REFERENCES invoice_rows(id) ON DELETE CASCADE,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            supplier_name TEXT, supplier_posting_account VARCHAR(100),
            nominal_account_code VARCHAR(100), invoice_number TEXT,
            invoice_date DATE, description TEXT,
            net_amount NUMERIC(14,2), vat_amount NUMERIC(14,2), total_amount NUMERIC(14,2),
            currency VARCHAR(20), tax_code VARCHAR(50),
            reviewed_fields TEXT, row_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by UUID REFERENCES users(id))""",
        "CREATE INDEX IF NOT EXISTS ix_corrections_batch ON invoice_row_corrections(batch_id)",
        "ALTER TABLE invoice_row_corrections ADD COLUMN IF NOT EXISTS scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_corrections_scan_run ON invoice_row_corrections(scan_run_id)",
        """UPDATE invoice_row_corrections c
        SET scan_run_id = b.current_scan_run_id
        FROM invoice_batches b
        WHERE c.scan_run_id IS NULL AND c.batch_id = b.id""",
        """CREATE TABLE IF NOT EXISTS invoice_row_field_audits (
            id BIGSERIAL PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            row_id BIGINT NOT NULL,
            field_name VARCHAR(80) NOT NULL,
            old_value TEXT, new_value TEXT,
            action VARCHAR(40) NOT NULL, note TEXT,
            rule_created BOOLEAN NOT NULL DEFAULT FALSE,
            force_added BOOLEAN NOT NULL DEFAULT FALSE,
            user_id UUID REFERENCES users(id), username VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_audits_batch_row ON invoice_row_field_audits(batch_id, row_id)",
        "ALTER TABLE invoice_row_field_audits ADD COLUMN IF NOT EXISTS scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_audits_scan_run ON invoice_row_field_audits(scan_run_id)",
        """UPDATE invoice_row_field_audits a
        SET scan_run_id = b.current_scan_run_id
        FROM invoice_batches b
        WHERE a.scan_run_id IS NULL AND a.batch_id = b.id""",
        """CREATE TABLE IF NOT EXISTS correction_rules (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
            rule_type VARCHAR(40) NOT NULL,
            field_name VARCHAR(80) NOT NULL,
            source_pattern TEXT NOT NULL,
            target_value TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            disabled_by UUID REFERENCES users(id),
            disabled_at TIMESTAMPTZ,
            origin_batch_id UUID,
            origin_row_id BIGINT,
            is_global BOOLEAN NOT NULL DEFAULT FALSE)""",
        "ALTER TABLE correction_rules ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS ix_rules_lookup ON correction_rules(tenant_id, rule_type, field_name, source_pattern, active)",
        "CREATE INDEX IF NOT EXISTS ix_rules_global_lookup ON correction_rules(is_global, rule_type, field_name, source_pattern, active)",
        """CREATE TABLE IF NOT EXISTS remap_hints (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
            supplier_id BIGINT REFERENCES tenant_suppliers(id) ON DELETE CASCADE,
            supplier_name_snapshot TEXT,
            field_name VARCHAR(80) NOT NULL,
            page_no INTEGER,
            x NUMERIC(8,4), y NUMERIC(8,4), w NUMERIC(8,4), h NUMERIC(8,4),
            source_batch_id UUID, source_file_id BIGINT, source_row_id BIGINT,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS stable_anchor_type VARCHAR(80)",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS stable_anchor_value TEXT",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS stable_anchor_evidence TEXT",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS archived_by UUID REFERENCES users(id)",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES users(id)",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS superseded_by_hint_id BIGINT",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_used_batch_id UUID",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_used_row_id BIGINT",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_used_page_no INTEGER",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_read_text TEXT",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_result TEXT",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS success_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS conflict_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS apply_count INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_remap_lookup ON remap_hints(supplier_id, field_name, active)",
        "CREATE INDEX IF NOT EXISTS ix_remap_governance ON remap_hints(tenant_id, company_id, supplier_id, field_name, active, archived, is_primary)",
        "CREATE INDEX IF NOT EXISTS ix_remap_lifecycle ON remap_hints(active, archived, deleted_at)",
        "CREATE INDEX IF NOT EXISTS ix_remap_stable_anchor ON remap_hints(tenant_id, company_id, stable_anchor_type, stable_anchor_value, field_name, active)",
        """CREATE TABLE IF NOT EXISTS batch_export_events (
            id BIGSERIAL PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            export_version INTEGER NOT NULL,
            template_id BIGINT,
            exported_by UUID REFERENCES users(id),
            exported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            file_path TEXT,
            file_bytes BYTEA,
            storage_backend VARCHAR(30) NOT NULL DEFAULT 'database+local',
            row_count INTEGER)""",
        "ALTER TABLE batch_export_events ADD COLUMN IF NOT EXISTS file_bytes BYTEA",
        "ALTER TABLE batch_export_events ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(30) NOT NULL DEFAULT 'database+local'",
        "ALTER TABLE batch_export_events ADD COLUMN IF NOT EXISTS scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_export_events_scan_run ON batch_export_events(scan_run_id)",
        """UPDATE batch_export_events e
        SET scan_run_id = b.current_scan_run_id
        FROM invoice_batches b
        WHERE e.scan_run_id IS NULL AND e.batch_id = b.id""",

        # ── invoice_field_candidates — future ML/ranking foundation ─────────
        """CREATE TABLE IF NOT EXISTS invoice_field_candidates (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            row_id BIGINT NOT NULL REFERENCES invoice_rows(id) ON DELETE CASCADE,
            source_file_id BIGINT REFERENCES invoice_files(id) ON DELETE SET NULL,
            field_name VARCHAR(80) NOT NULL,
            candidate_value TEXT,
            normalised_value TEXT,
            source_type VARCHAR(80) NOT NULL,
            source_id TEXT,
            confidence NUMERIC(6,4),
            evidence TEXT,
            reason TEXT,
            selected BOOLEAN NOT NULL DEFAULT FALSE,
            applied BOOLEAN NOT NULL DEFAULT FALSE,
            rejected_reason TEXT,
            conflict BOOLEAN NOT NULL DEFAULT FALSE,
            user_accepted BOOLEAN NOT NULL DEFAULT FALSE,
            user_corrected BOOLEAN NOT NULL DEFAULT FALSE,
            final_value TEXT,
            finalised_at TIMESTAMPTZ,
            finalised_by UUID REFERENCES users(id),
            outcome_source VARCHAR(40),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS user_accepted BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS user_corrected BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS final_value TEXT",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS finalised_at TIMESTAMPTZ",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS finalised_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS outcome_source VARCHAR(40)",
        "ALTER TABLE invoice_field_candidates ADD COLUMN IF NOT EXISTS scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_tenant_company ON invoice_field_candidates(tenant_id, company_id)",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_batch_row ON invoice_field_candidates(batch_id, row_id)",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_field_name ON invoice_field_candidates(field_name)",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_source_type ON invoice_field_candidates(source_type)",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_selected ON invoice_field_candidates(selected)",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_created_at ON invoice_field_candidates(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_field_candidates_scan_run ON invoice_field_candidates(scan_run_id)",
        """UPDATE invoice_field_candidates c
        SET scan_run_id = b.current_scan_run_id
        FROM invoice_batches b
        WHERE c.scan_run_id IS NULL AND c.batch_id = b.id""",
        """CREATE TABLE IF NOT EXISTS invoice_duplicate_candidates (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_id UUID REFERENCES companies(id) ON DELETE SET NULL,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            row_id BIGINT NOT NULL REFERENCES invoice_rows(id) ON DELETE CASCADE,
            candidate_batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            candidate_scan_run_id UUID REFERENCES scan_runs(id) ON DELETE SET NULL,
            candidate_row_id BIGINT NOT NULL REFERENCES invoice_rows(id) ON DELETE CASCADE,
            match_type VARCHAR(40) NOT NULL DEFAULT 'cross_batch',
            match_status VARCHAR(40) NOT NULL,
            confidence NUMERIC(6,4),
            evidence_json TEXT,
            normalized_invoice_number VARCHAR(160),
            document_type VARCHAR(80),
            supplier_key VARCHAR(255),
            supplier_vat VARCHAR(100),
            invoice_date DATE,
            total_cents BIGINT,
            currency VARCHAR(20),
            document_fingerprint VARCHAR(80),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ,
            resolved_by UUID REFERENCES users(id))""",
        "CREATE INDEX IF NOT EXISTS ix_duplicate_candidates_tenant_company ON invoice_duplicate_candidates(tenant_id, company_id)",
        "CREATE INDEX IF NOT EXISTS ix_duplicate_candidates_batch_row ON invoice_duplicate_candidates(batch_id, row_id)",
        "CREATE INDEX IF NOT EXISTS ix_duplicate_candidates_candidate_row ON invoice_duplicate_candidates(candidate_batch_id, candidate_row_id)",
        "CREATE INDEX IF NOT EXISTS ix_duplicate_candidates_scan_run ON invoice_duplicate_candidates(scan_run_id)",
        "CREATE INDEX IF NOT EXISTS ix_duplicate_candidates_status ON invoice_duplicate_candidates(match_status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_duplicate_candidates_pair_type ON invoice_duplicate_candidates(row_id, candidate_row_id, match_type)",
        "CREATE INDEX IF NOT EXISTS ix_export_events_batch ON batch_export_events(batch_id)",
        # <<< REVIEW_PACK startup_alters


        # ── export_templates ──────────────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS export_templates ("
            "id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            "name VARCHAR(255) NOT NULL,"
            "description TEXT,"
            "accounting_system VARCHAR(100),"
            "version_label VARCHAR(50) NOT NULL DEFAULT 'v1',"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "is_system_default BOOLEAN NOT NULL DEFAULT FALSE,"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "created_by UUID REFERENCES users(id) ON DELETE SET NULL,"
            "updated_by UUID REFERENCES users(id) ON DELETE SET NULL"
            ")"
        ),

        # ── export_template_columns ───────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS export_template_columns ("
            "id SERIAL PRIMARY KEY,"
            "template_id UUID NOT NULL REFERENCES export_templates(id) ON DELETE CASCADE,"
            "column_order INTEGER NOT NULL DEFAULT 0,"
            "column_heading VARCHAR(255) NOT NULL,"
            "column_type VARCHAR(50) NOT NULL,"
            "source_field VARCHAR(100),"
            "static_value VARCHAR(500),"
            "transform_rule VARCHAR(200),"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "notes TEXT"
            ")"
        ),

        # ── export_template_columns — new columns ─────────────────────────────
        "ALTER TABLE export_template_columns ADD COLUMN IF NOT EXISTS condition_rules JSONB",

        # ── template_assignments ──────────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS template_assignments ("
            "id SERIAL PRIMARY KEY,"
            "template_id UUID NOT NULL REFERENCES export_templates(id) ON DELETE CASCADE,"
            "tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
            "company_id UUID REFERENCES companies(id) ON DELETE CASCADE,"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "assigned_by UUID REFERENCES users(id) ON DELETE SET NULL"
            ")"
        ),

        # ── admin_audit_logs ──────────────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS admin_audit_logs ("
            "id SERIAL PRIMARY KEY,"
            "event_type VARCHAR(100) NOT NULL,"
            "entity_type VARCHAR(100) NOT NULL,"
            "entity_id VARCHAR(255),"
            "user_id UUID REFERENCES users(id) ON DELETE SET NULL,"
            "notes TEXT,"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        ),
    ]

    ok = skipped = 0
    for stmt in statements:
        # Each statement gets its own connection + transaction so that one
        # failure never puts subsequent statements into an aborted transaction.
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            ok += 1
        except Exception as stmt_exc:
            skipped += 1
            logger.debug("Schema migration skipped (%s): %.120s", type(stmt_exc).__name__, stmt)

    logger.info("ensure_runtime_schema: %d applied, %d already-present/skipped", ok, skipped)


try:
    ensure_runtime_schema()
except Exception as exc:
    logger.warning("ensure_runtime_schema failed (non-fatal): %s", exc)

app = FastAPI(title=settings.app_name)
base_dir = Path(__file__).resolve().parent
static_dir = base_dir / "static"
_version_file = base_dir.parent / "VERSION"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
async def recover_stuck_batches() -> None:
    from app.db.session import SessionLocal
    from app.db.models import InvoiceBatch
    db = SessionLocal()
    try:
        stuck = db.query(InvoiceBatch).filter(InvoiceBatch.status == "processing").all()
        for batch in stuck:
            batch.status = "partial"
            batch.notes = "Processing was interrupted by a server restart. Re-process to complete."
            batch.processed_at = datetime.now(timezone.utc)
        if stuck:
            db.commit()
            logger.info("Recovered %d stuck batch(es) from 'processing' status on startup", len(stuck))
    except Exception as exc:
        logger.warning("Failed to recover stuck batches on startup: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@app.on_event("startup")
async def run_file_retention_cleanup() -> None:
    """Delete uploaded PDFs and exported XLSXs older than the configured
    retention window (default 5 days).  Runs once at startup so the server
    self-cleans on every deploy/restart without needing a cron job.

    Safety rules:
    - Only files strictly older than file_retention_days are removed.
    - Whole batch-upload folders are removed only when ALL files inside them
      are past the retention window (avoids breaking active review sessions).
    - Export files are removed individually by mtime.
    - DB records are not touched — rows remain for analytics/audit history.
    """
    import time
    import shutil
    from pathlib import Path as _Path

    retention_seconds = settings.file_retention_days * 86_400
    now = time.time()
    removed_files = removed_folders = 0

    # ── Uploaded batch folders ────────────────────────────────────────────────
    try:
        upload_root = _Path(settings.upload_dir).resolve()
        if upload_root.exists():
            for batch_folder in upload_root.iterdir():
                if not batch_folder.is_dir():
                    continue
                pdf_files = [f for f in batch_folder.rglob("*") if f.is_file()]
                if not pdf_files:
                    try:
                        shutil.rmtree(batch_folder, ignore_errors=True)
                        removed_folders += 1
                    except Exception:
                        pass
                    continue
                newest_mtime = max(f.stat().st_mtime for f in pdf_files)
                if (now - newest_mtime) > retention_seconds:
                    try:
                        shutil.rmtree(batch_folder, ignore_errors=True)
                        removed_folders += 1
                    except Exception as exc:
                        logger.warning("Retention: could not remove folder %s: %s", batch_folder, exc)
    except Exception as exc:
        logger.warning("Retention: upload cleanup failed: %s", exc)

    # ── Exported XLSX files ───────────────────────────────────────────────────
    try:
        export_root = _Path(settings.export_dir).resolve()
        if export_root.exists():
            for export_file in export_root.rglob("*.xlsx"):
                if not export_file.is_file():
                    continue
                try:
                    if (now - export_file.stat().st_mtime) > retention_seconds:
                        export_file.unlink(missing_ok=True)
                        removed_files += 1
                except Exception as exc:
                    logger.warning("Retention: could not remove export %s: %s", export_file, exc)
    except Exception as exc:
        logger.warning("Retention: export cleanup failed: %s", exc)

    if removed_folders or removed_files:
        logger.info(
            "Retention cleanup: removed %d batch folder(s), %d export file(s) "
            "(retention=%d days)",
            removed_folders, removed_files, settings.file_retention_days,
        )



@app.get("/version")
def get_version():
    version = _version_file.read_text().strip() if _version_file.exists() else "0.0.0"
    return JSONResponse({"version": version})


@app.get("/")
def frontend():
    path = static_dir / "login.html"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=500, detail=f"Frontend file not found: {path}")


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(batches.router)
# >>> REVIEW_PACK router_register
from app.routers import review as _review_router
app.include_router(_review_router.router)
# <<< REVIEW_PACK router_register
app.include_router(admin.router)
app.include_router(admin_export_templates.router)
app.include_router(tenant.router)
app.include_router(analytics.router)
