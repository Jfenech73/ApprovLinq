from __future__ import annotations

from types import SimpleNamespace

import app.routers.batches as batches


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows
    def filter(self, *args, **kwargs):
        return self
    def order_by(self, *args, **kwargs):
        return self
    def all(self):
        return self.rows


class _FakeScalar:
    def scalar_one_or_none(self):
        return None


class _FakeDB:
    def __init__(self, hints):
        self.hints = hints
    def query(self, model):
        return _FakeQuery(self.hints)
    def execute(self, *args, **kwargs):
        return _FakeScalar()
    def get(self, model, ident):
        name = getattr(model, "__name__", "")
        if name == "InvoiceFile":
            return SimpleNamespace(file_path="dummy.pdf")
        return None


def _hint(snapshot: str):
    return SimpleNamespace(
        id=1,
        field_name="supplier_name",
        supplier_name_snapshot=snapshot,
        supplier_id=None,
        page_no=1,
        x=0.10,
        y=0.10,
        w=0.20,
        h=0.05,
        source_row_id=None,
        active=True,
    )


def _row(name: str | None):
    return SimpleNamespace(
        supplier_name=name,
        invoice_number="INV-1",
        invoice_date="2026-01-01",
        net_amount=10.0,
        vat_amount=0.0,
        total_amount=10.0,
        nominal_account_code="5000",
        description="Fruit",
        review_required=False,
        review_fields=None,
        confidence_score=0.90,
        page_no=1,
        source_file_id=123,
        method_used="ocr",
        header_raw="Mafimex Ltd. The Fruit and Vegetable Centre",
        totals_raw="Total Gross 10.00",
        page_text_raw="Mafimex Ltd. The Fruit and Vegetable Centre INVOICE Total Gross 10.00",
    )


def test_supplier_saved_region_confirms_partial_legal_name_even_when_region_ocr_blank(monkeypatch):
    row = _row("Mafimex")
    db = _FakeDB([_hint("Mafimex Ltd.")])

    monkeypatch.setattr("app.utils.storage.resolve_upload_path", lambda p: "dummy.pdf")
    monkeypatch.setattr("app.routers.review._read_region_text", lambda *a, **k: "")

    batches._apply_remap_hints(db, SimpleNamespace(tenant_id="t1", company_id="c1"), row)

    assert row.supplier_name == "Mafimex Ltd."
    assert "remap_hint:supplier_name" in row.method_used


def test_supplier_saved_region_cleans_marketing_year_prefix(monkeypatch):
    row = _row("35 Nectar Limited")
    row.header_raw = "35 Nectar Limited Cash sale"
    row.page_text_raw = "35 Nectar Limited Cash sale Total Due 93.75"
    db = _FakeDB([_hint("Nectar Limited")])

    monkeypatch.setattr("app.utils.storage.resolve_upload_path", lambda p: "dummy.pdf")
    monkeypatch.setattr("app.routers.review._read_region_text", lambda *a, **k: "Nectar Limited")

    batches._apply_remap_hints(db, SimpleNamespace(tenant_id="t1", company_id="c1"), row)

    assert row.supplier_name == "Nectar Limited"


def test_supplier_saved_region_does_not_overwrite_unrelated_strong_supplier(monkeypatch):
    row = _row("Strong Supplier Ltd")
    row.header_raw = "Strong Supplier Ltd INVOICE"
    row.page_text_raw = "Strong Supplier Ltd INVOICE Total 10.00"
    db = _FakeDB([_hint("Other Supplier Ltd")])

    monkeypatch.setattr("app.utils.storage.resolve_upload_path", lambda p: "dummy.pdf")
    monkeypatch.setattr("app.routers.review._read_region_text", lambda *a, **k: "Other Supplier Ltd")

    batches._apply_remap_hints(db, SimpleNamespace(tenant_id="t1", company_id="c1"), row)

    assert row.supplier_name == "Strong Supplier Ltd"
