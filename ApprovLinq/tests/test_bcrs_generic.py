import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.routers.batches import _extract_bcrs_amount_from_summary


def test_bcrs_same_line_totals_label():
    payload = {
        'totals_raw': 'Net 132.00\nVAT 23.76\nBCRS 14.40\nTotal EUR 170.16',
        'net_amount': 132.00,
        'vat_amount': 23.76,
        'total_amount': 170.16,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 14.40


def test_bcrs_summary_box_deposit_label():
    payload = {
        'totals_raw': 'Subtotal: 526.22\nVat: 94.72\nBCRS Deposit: 70.80\nTotal: 691.74',
        'net_amount': 526.22,
        'vat_amount': 94.72,
        'total_amount': 691.74,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 70.80


def test_bcrs_refundable_deposit_style_label():
    payload = {
        'page_text_raw': '...\nTotal Net Amount 77.52\nVAT Amount 10.40\n* BCRS Refundable Deposit (M) € 10.80\nInvoice Total 98.72\n',
        'net_amount': 77.52,
        'vat_amount': 10.40,
        'total_amount': 98.72,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 10.80


def test_plain_deposit_in_item_rows_not_accepted():
    payload = {
        'page_text_raw': 'Item Description Qty Unit Price\nBCRS PET 24 1.00 2.00\nAnother line deposit 24 0.10\n',
        'net_amount': 100.00,
        'vat_amount': 23.00,
        'total_amount': 123.00,
    }
    assert _extract_bcrs_amount_from_summary(payload) is None


def test_reconciliation_boost_breaks_tie():
    payload = {
        'totals_raw': 'Net 61.80\nVAT 11.12\nBCRS 7.20\nTotal 80.12\nOther charge 2.40',
        'net_amount': 61.80,
        'vat_amount': 11.12,
        'total_amount': 80.12,
    }
    assert _extract_bcrs_amount_from_summary(payload) == 7.20
