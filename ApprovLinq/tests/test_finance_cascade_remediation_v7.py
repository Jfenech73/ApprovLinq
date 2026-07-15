import io
import shutil

import pytest
from PIL import Image, ImageDraw

from app.services.extractor import (
    _apply_financial_remediation,
    _collect_supplier_candidates,
    _extract_labeled_financial_bundle,
    simple_extract,
)
from app.routers.review import _ocr_region_bytes, _normalise_supplier_remap_text


def test_vat_rate_is_not_used_as_vat_amount_on_dione_style_summary():
    text = "Subtotal 2,917.67\nVAT 18% 525.18\nTOTAL 3,442.85"
    assert _extract_labeled_financial_bundle(text) == {
        "net_amount": 2917.67,
        "vat_amount": 525.18,
        "total_amount": 3442.85,
    }
    result = simple_extract(text)
    assert result["net_amount"] == 2917.67
    assert result["vat_amount"] == 525.18
    assert result["total_amount"] == 3442.85


def test_nectar_style_vat_line_uses_last_money_not_taxable_base():
    text = "Net Total: 14.05\nVAT € 18% 14.05 2.51\nBCRS 0.00\nTotal Due EUR 16.56"
    result = simple_extract(text)
    assert result["net_amount"] == 14.05
    assert result["vat_amount"] == 2.51
    assert result["total_amount"] == 16.56
    assert not result["supplier_name"]


def test_financial_remediation_repairs_bad_di_amount_bundle():
    extracted = {"net_amount": 917.67, "vat_amount": 18.0, "total_amount": 3442.85}
    text = "Subtotal 2,917.67\nVAT 18% 525.18\nTOTAL 3,442.85"
    fixed = _apply_financial_remediation(extracted, text, "di_text_reconciliation")
    assert fixed["net_amount"] == 2917.67
    assert fixed["vat_amount"] == 525.18
    assert fixed["total_amount"] == 3442.85
    assert fixed["_field_sources"]["vat_amount"] == "di_text_reconciliation"


def test_supplier_candidates_keep_contact_block_legal_name_before_tagline():
    text = """Mafimex Ltd.\nThe Fruit and Vegetable Centre\nPage 1 of 1\na: Trig L-Idward, Ta' Qali\nt: +356 21493362\ne: sales@mafimex.com\nINVOICE"""
    assert _collect_supplier_candidates(text)[0] == "Mafimex Ltd."


def test_region_ocr_bytes_returns_visible_text():
    if not shutil.which("tesseract"):
        pytest.skip("local Tesseract binary not available")
    img = Image.new("RGB", (420, 90), "white")
    draw = ImageDraw.Draw(img)
    draw.text((12, 24), "Mafimex Ltd.", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    text = _ocr_region_bytes(buf.getvalue())
    assert "Mafimex" in text or "Mafimex Ltd" in text


def test_supplier_remap_text_keeps_legal_name_from_collapsed_crop():
    text = "Mafimex Ltd. The Fruit and Vegetable Centre a: Trig L-Idward, Ta Qali t: +356 21493362"
    assert _normalise_supplier_remap_text(text) == "Mafimex Ltd"
