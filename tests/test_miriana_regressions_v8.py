from __future__ import annotations

from pathlib import Path

from app.services.extractor import (
    _clean_ocr_supplier_name,
    _extract_labeled_financial_bundle,
    _collect_invoice_number_candidates,
    simple_extract,
)
from app.routers.review import _normalise_supplier_remap_text, _promote_supplier_remap_text


def test_n_calleja_vertical_summary_and_invoice_label_are_read_correctly():
    text = """
    N.Calleja Trading Ent. Ltd.
    Telephone No: 21462227
    Invoice 10512630
    Net Amount
    VAT Amount
    VAT Summary
    mount
    67.98
    E 0%
    67.98
    0.00
    VAT Amount
    0.00
    Total Amount in EUR
    67.98
    """
    result = simple_extract(text)
    assert result["invoice_number"] == "10512630"
    assert result["net_amount"] == 67.98
    assert result["vat_amount"] == 0.0
    assert result["total_amount"] == 67.98


def test_near_truncated_net_is_repaired_from_total_when_vat_zero():
    bundle = _extract_labeled_financial_bundle(
        "Net Amount 67.9\nVAT Amount 0.00\nTotal Amount in EUR 67.98"
    )
    assert bundle["net_amount"] == 67.98
    assert bundle["vat_amount"] == 0.0
    assert bundle["total_amount"] == 67.98


def test_nectar_supplier_logo_year_prefix_removed():
    assert _clean_ocr_supplier_name("35 Nectar Limited") == "Nectar Limited"
    assert simple_extract("35 Nectar Limited\nCash sale\nNet Total: 93.75\nTotal Due: 93.75")["supplier_name"] == "Nectar Limited"


def test_nectar_zero_vat_column_layout_not_read_as_net_again():
    text = """
    35 Nectar Limited
    Net Total:
    93.75
    VAT:
    93.75
    0.00
    E@0%
    BCRS Deposit:
    0.00
    Total Due:
    EUR
    93.75
    """
    result = simple_extract(text)
    assert result["supplier_name"] == "Nectar Limited"
    assert result["net_amount"] == 93.75
    assert result["vat_amount"] == 0.0
    assert result["total_amount"] == 93.75


def test_nectar_bcrs_vat_layout_still_reads_vat_and_deposit():
    text = """
    35 Nectar Limited
    Net Total: 14.05
    VAT
    € 18%
    14.05
    2.51
    BCRS Deposit:
    0.60
    Total Due: EUR 16.56
    """
    result = simple_extract(text)
    assert result["supplier_name"] == "Nectar Limited"
    assert result["net_amount"] == 14.05
    assert result["vat_amount"] == 2.51
    assert result["_deposit_candidate"] == 0.60
    assert result["total_amount"] == 16.56


def test_mafimex_supplier_keeps_legal_suffix_from_header():
    text = """
    Mafimex Ltd.
    The Fruit and Vegetable Centre
    a: Trig L-Idward, Ta' Qali, Limits of Attard
    t: +356 21493362 / 21414318
    e:sales@mafimex.com
    INVOICE
    Total Net (€) 68.55
    Total VAT (€) 0.00
    Total Gross (€) 68.55
    """
    result = simple_extract(text)
    assert result["supplier_name"] == "Mafimex Ltd."
    assert result["net_amount"] == 68.55
    assert result["vat_amount"] == 0.0
    assert result["total_amount"] == 68.55


def test_supplier_remap_text_promotes_collapsed_mafimex_crop():
    assert _normalise_supplier_remap_text(
        "Mafimex Ltd. The Fruit and Vegetable Centre a: Trig L-Idward"
    ) == "Mafimex Ltd"


def test_promote_supplier_remap_text_uses_full_page_when_region_is_partial(tmp_path: Path):
    import fitz

    pdf_path = tmp_path / "mafimex.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72), "Mafimex Ltd.", fontsize=22)
    page.insert_text((72, 104), "The Fruit and Vegetable Centre", fontsize=12)
    page.insert_text((72, 132), "t: +356 21493362   e: sales@mafimex.com", fontsize=10)
    page.insert_text((72, 170), "INVOICE", fontsize=20)
    doc.save(pdf_path)
    doc.close()

    promoted = _promote_supplier_remap_text(str(pdf_path), 1, "Mafimex")
    assert promoted.startswith("Mafimex")
    assert "Ltd" in promoted


def test_invoice_number_candidate_prefers_plain_invoice_label_over_phone_number():
    candidates = _collect_invoice_number_candidates(
        "Telephone No: 21462227\nInvoice 10512630\nVAT Reg No MT12345678"
    )
    assert candidates[0] == "10512630"
