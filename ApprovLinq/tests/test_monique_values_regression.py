from pathlib import Path

from app.services.extractor import _extract_labeled_financial_bundle

ROOT = Path(__file__).resolve().parents[1]


def test_derive_net_when_total_and_vat_present_but_net_missing():
    bundle = _extract_labeled_financial_bundle(
        "VAT 18%\nTax\n15.99\nTotal\n104.83\nDue\n104.83"
    )
    assert bundle["net_amount"] == 88.84
    assert bundle["vat_amount"] == 15.99
    assert bundle["total_amount"] == 104.83


def test_repair_net_duplicated_from_total_on_vat_invoice():
    bundle = _extract_labeled_financial_bundle(
        "Sub Total\n147.23\nVAT 18%\nVAT\n24.77\nTotal\n172.00\nBalance Due\n172.00"
    )
    assert bundle["net_amount"] == 147.23
    assert bundle["vat_amount"] == 24.77
    assert bundle["total_amount"] == 172.00


def test_bad_vat_candidate_repaired_from_18_percent_total():
    bundle = _extract_labeled_financial_bundle(
        "VAT 18%\nTax\n185.00\nTotal\n754.68\nDue\n754.68"
    )
    assert bundle["net_amount"] == 639.56
    assert bundle["vat_amount"] == 115.12
    assert bundle["total_amount"] == 754.68


def test_due_label_can_supply_total_when_total_line_is_fragmented():
    bundle = _extract_labeled_financial_bundle(
        "Total\n$9.67\nPayment Total\n0.00\nDue\n89.67"
    )
    assert bundle["total_amount"] == 89.67


def test_surcharge_alone_is_not_bcrs_split_evidence():
    from app.routers.batches import _extract_bcrs_amount_from_summary

    assert _extract_bcrs_amount_from_summary({
        "totals_raw": "Net 100.00\nVAT 18.00\nSurcharge 2.00\nTotal 120.00",
        "page_text_raw": "Surcharge 2.00",
        "net_amount": 100.0,
        "vat_amount": 18.0,
        "total_amount": 120.0,
    }) is None
