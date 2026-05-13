from app.services.extractor import _extract_labeled_financial_bundle, _apply_financial_remediation


def test_zero_vat_total_bcrs_zero_does_not_create_amount():
    text = """
Vat Rate
Ex VAT
VAT Amt
Inc VAT
Total Eur
90.88
0 00 (L)
98 88
0.00
96.85
Total VAT
0.00
Totale:
90.88
0.00
98.85
Total BCRS
0.00
CASH SALE
Total amount to pay
98.88
"""
    bundle = _extract_labeled_financial_bundle(text)
    assert bundle["net_amount"] == 98.88
    assert bundle["vat_amount"] == 0.0
    assert bundle["total_amount"] == 98.88


def test_summary_grid_reconciles_repeated_before_tax_tax_total_labels():
    text = """
Invoice Lines
Additional Charges
Total Invoice
Before Tax
248 33
Before Tax
0.00
Before Tax
248.33
T **
44.71
Tax
0 00
Tax
44.7:
Total
291 01
Total
0.00
Total
193.04
Payment Total
0.00
Due
293.04
"""
    bundle = _extract_labeled_financial_bundle(text)
    assert bundle["net_amount"] == 248.33
    assert bundle["vat_amount"] == 44.71
    assert bundle["total_amount"] == 293.04


def test_due_line_does_not_override_reconciled_total_grid():
    text = """
Before Tax
11: 65
Before Tax
0.00
Before Tax
11: 45
Tax
20 10
Tax
0.00
Tax
20 10
Total
131 75
Total
0.00
Total
131.75
Payment Total
0 00
Dun
181.75
"""
    bundle = _extract_labeled_financial_bundle(text)
    assert bundle["net_amount"] == 111.65
    assert bundle["vat_amount"] == 20.10
    assert bundle["total_amount"] == 131.75


def test_vat_table_does_not_treat_total_inc_vat_as_vat_amount():
    text = """
Vat Rate
Ex VAT
VAT Amt
Inc VAT
Total exc VAT
115 65
18.00 (F)
115.65
20.82
136.47
VAT Amount
20 82
Totals:
115.55
20.82
136 47
Total inc VAT
135.47
Grand Total
136.47
"""
    bundle = _extract_labeled_financial_bundle(text)
    assert bundle["net_amount"] == 115.65
    assert bundle["vat_amount"] == 20.82
    assert bundle["total_amount"] == 136.47


def test_tax_column_subtotal_is_reinterpreted_as_vat_when_total_is_gross():
    text = """
Sub Total
24.77
172.00
Total
€172.00
Balance Due
€172.00
Tax
18.00%
"""
    bundle = _extract_labeled_financial_bundle(text)
    assert bundle["net_amount"] == 147.23
    assert bundle["vat_amount"] == 24.77
    assert bundle["total_amount"] == 172.00


def test_remediation_keeps_already_reconciled_amounts_over_bad_labelled_bundle():
    current = {"net_amount": 4.53, "vat_amount": 0.82, "total_amount": 5.35}
    text = """
VAT Amount
0 82
Totals:
4.53
0.82
5.35
Total inc VAT
5.35
Grand Total
5.35
"""
    out = _apply_financial_remediation(dict(current), text, "test")
    assert out["net_amount"] == 4.53
    assert out["vat_amount"] == 0.82
    assert out["total_amount"] == 5.35
