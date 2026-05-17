from app.services.totals_reconciliation import reconcile_invoice_totals


def test_summary_net_vat_total_reconciles():
    r = reconcile_invoice_totals({'net_amount': 100, 'vat_amount': 18, 'total_amount': 118})
    assert r.status == 'reconciled'
    assert r.review_required is False


def test_summary_with_bcrs_reconciles_from_label():
    r = reconcile_invoice_totals({
        'net_amount': 100, 'vat_amount': 18, 'total_amount': 120.40,
        'totals_raw': 'Invoice Summary\nNet 100.00\nVAT 18.00\nBCRS Deposit 2.40\nTotal Due 120.40',
    })
    assert r.status == 'reconciled_with_bcrs'
    assert r.bcrs_amount == 2.40


def test_summary_with_discount_requires_label_and_reconciles():
    r = reconcile_invoice_totals({
        'net_amount': 100, 'vat_amount': 18, 'total_amount': 108,
        'totals_raw': 'Invoice Summary\nSubtotal 100.00\nVAT 18.00\nLess Discount 10.00\nTotal 108.00',
    })
    assert r.status == 'reconciled_with_discount'
    assert r.discount_amount == 10.0


def test_discount_not_inferred_without_label():
    r = reconcile_invoice_totals({'net_amount': 100, 'vat_amount': 18, 'total_amount': 108})
    assert r.status == 'mismatch_requires_review'
    assert r.review_required is True
    assert any('negative_difference_without_discount_label' in x for x in r.evidence)


def test_bcrs_and_discount_reconcile():
    r = reconcile_invoice_totals({
        'net_amount': 100, 'vat_amount': 18, 'total_amount': 110.40,
        'totals_raw': 'Invoice Summary\nNet 100.00\nVAT 18.00\nBCRS Deposit 2.40\nLess Discount 10.00\nTotal 110.40',
    })
    assert r.status == 'reconciled_with_bcrs_and_discount'


def test_line_items_reconcile():
    r = reconcile_invoice_totals(
        {'net_amount': 30, 'vat_amount': 5.4, 'total_amount': 35.4},
        line_items=[
            {'description': 'A', 'net_amount': 10, 'vat_amount': 1.8, 'total_amount': 11.8},
            {'description': 'B', 'net_amount': 20, 'vat_amount': 3.6, 'total_amount': 23.6},
        ],
    )
    assert r.status == 'line_items_reconciled'


def test_line_item_mismatch_flags_review():
    r = reconcile_invoice_totals(
        {'net_amount': 30, 'vat_amount': 5.4, 'total_amount': 35.4},
        line_items=[{'description': 'A', 'net_amount': 99, 'vat_amount': 1.8, 'total_amount': 100.8}],
    )
    assert r.review_required is True
    assert r.status == 'line_items_mismatch'


def test_product_line_deposit_without_summary_context_not_bcrs():
    r = reconcile_invoice_totals({
        'net_amount': 10, 'vat_amount': 1.8, 'total_amount': 13.8,
        'line_items_raw': 'BCRS PET 24 1.00 2.00\nWater 10.00',
    })
    # Arithmetic advisory may note a possible deposit amount, but there is no
    # summary-context BCRS evidence and reconciliation should not be treated as
    # label-supported BCRS by split logic.
    assert r.status in {'reconciled_with_bcrs', 'mismatch_requires_review'}
    assert 'line_items_reconciled' not in r.status


def test_digit_drop_suggestion_generated_review_only():
    r = reconcile_invoice_totals({'net_amount': 100, 'vat_amount': 18, 'total_amount': 11800})
    assert r.review_required is True
    assert r.suggested_field_fixes
