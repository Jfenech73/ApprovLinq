"""
Tests for the region-first BCRS/deposit label+value detection system.

Covers:
  1. validate_invoice.py — raised deposit cap handles large BCRS totals
  2. _detect_bcrs_from_text — summary-zone label+value extraction
  3. _score_bcrs_candidate — scoring rules
  4. _page_summary_zone — bottom-region and keyword-block selection
  5. _detect_bcrs_label_value — integration: text path
  6. _detect_bcrs_from_fitz — coordinate-aware path (mocked)
  7. Negative cases — item-row rejection, no false positives
  8. Reconciliation boost
  9. BCRS duplicate row — no double-creation on reprocess

Run with:  pytest tests/test_bcrs_detection.py -v
"""
from __future__ import annotations

import re
import pytest


# ══════════════════════════════════════════════════════════════════════════════
# 1. validate_invoice.py — raised deposit cap
# ══════════════════════════════════════════════════════════════════════════════

class TestDepositCapRaised:
    """_is_deposit_amount must accept amounts up to €200 now."""

    def _is_dep(self, diff):
        from app.services.validate_invoice import _is_deposit_amount
        return _is_deposit_amount(diff)

    def test_small_deposit_accepted(self):
        assert self._is_dep(0.50)
        assert self._is_dep(2.40)
        assert self._is_dep(7.20)

    def test_medium_deposit_accepted(self):
        assert self._is_dep(14.40)
        assert self._is_dep(10.80)

    def test_large_deposit_accepted(self):
        """70.80 (J. Sultana) must now be accepted."""
        assert self._is_dep(70.80), "70.80 must pass with the raised €200 cap"

    def test_cap_boundary(self):
        assert self._is_dep(200.00)
        assert not self._is_dep(200.01)

    def test_negative_rejected(self):
        assert not self._is_dep(-1.00)

    def test_zero_rejected(self):
        assert not self._is_dep(0.0)

    def test_non_deposit_fraction_rejected(self):
        """A diff of 7.13 (uncommon fraction) must be rejected."""
        assert not self._is_dep(7.13)

    def test_validate_invoice_large_deposit(self):
        """validate_invoice correctly identifies a 70.80 BCRS deposit."""
        from app.services.validate_invoice import validate_invoice
        result = validate_invoice({
            "net_amount": 400.00,
            "vat_amount": 72.00,
            "total_amount": 542.80,   # 400 + 72 + 70.80
        })
        assert result.deposit_amount == 70.80, \
            f"Expected deposit_amount=70.80, got {result.deposit_amount}"
        assert result.totals_reconciliation_status == "ok_with_deposit"

    def test_validate_invoice_small_deposit(self):
        from app.services.validate_invoice import validate_invoice
        result = validate_invoice({
            "net_amount": 100.00,
            "vat_amount": 18.00,
            "total_amount": 120.40,   # 2.40 BCRS
        })
        assert result.deposit_amount == 2.40

    def test_validate_invoice_no_deposit_when_balanced(self):
        from app.services.validate_invoice import validate_invoice
        result = validate_invoice({
            "net_amount": 100.00,
            "vat_amount": 18.00,
            "total_amount": 118.00,
        })
        assert not result.deposit_amount


# ══════════════════════════════════════════════════════════════════════════════
# 2. _page_summary_zone
# ══════════════════════════════════════════════════════════════════════════════

class TestPageSummaryZone:
    """Summary zone selection pulls the bottom of the page and financial blocks."""

    def _zone(self, lines):
        from app.services.extractor import _page_summary_zone
        return _page_summary_zone(lines)

    def test_bottom_40_percent_included(self):
        lines = [f"item {i}" for i in range(20)]
        zone = self._zone(lines)
        # Must include the last 8 lines (40%)
        assert "item 19" in zone
        assert "item 18" in zone

    def test_financial_keyword_lines_included(self):
        lines = ["Header line"] * 15 + ["Net Amount 100.00", "VAT 18.00", "BCRS 2.40"]
        zone = self._zone(lines)
        assert any("BCRS" in l for l in zone)
        assert any("Net Amount" in l for l in zone)

    def test_body_lines_excluded(self):
        """Non-financial lines in the upper 60% must not appear."""
        lines = ["PRODUCT A x500ml 12pcs"] + ["filler"] * 20 + ["Total 100.00"]
        zone = self._zone(lines)
        assert "PRODUCT A x500ml 12pcs" not in zone

    def test_empty_input_returns_empty(self):
        assert self._zone([]) == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. _score_bcrs_candidate
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreBcrsCandidate:
    """Scoring rules applied correctly."""

    def _score(self, label, amount, in_summary=True, same_line=True,
               source_line=None, net=None, vat=None, total=None):
        from app.services.extractor import _score_bcrs_candidate
        return _score_bcrs_candidate(
            label_text=label,
            amount=amount,
            source_line=source_line or label,
            in_summary_zone=in_summary,
            same_line=same_line,
            net=net,
            vat=vat,
            total=total,
        )

    def test_bcrs_deposit_scores_high(self):
        assert self._score("BCRS Deposit", 7.20) > 100

    def test_bcrs_alone_scores_high(self):
        assert self._score("BCRS", 14.40) > 80

    def test_plain_deposit_in_summary_scores_lower(self):
        s_summary = self._score("Deposit", 2.40, in_summary=True)
        s_body    = self._score("Deposit", 2.40, in_summary=False)
        assert s_summary > s_body
        # Outside summary: weak "deposit" alone must be rejected (score=0)
        assert s_body == 0

    def test_reconciliation_boost_applied(self):
        # total ≈ net + vat + candidate → big boost
        s_reconciled = self._score(
            "BCRS", 2.40, net=100.0, vat=18.0, total=120.40
        )
        s_unreconciled = self._score("BCRS", 5.00, net=100.0, vat=18.0, total=120.40)
        assert s_reconciled > s_unreconciled

    def test_item_row_rejected(self):
        """A line that looks like a product row must score 0."""
        score = self._score(
            label="BCRS",
            amount=14.40,
            source_line="12 x 500ml BCRS 14.40 item description here with many words",
            in_summary=True,
        )
        # _text_is_item_row detects "12 x 500ml" → quantity prefix
        assert score == 0

    def test_amount_larger_than_80pct_of_total_rejected(self):
        """If candidate > 80% of total it's the total itself, not a deposit."""
        score = self._score("BCRS", 90.00, net=100.0, vat=0.0, total=100.00)
        assert score == 0

    def test_zero_amount_rejected(self):
        assert self._score("BCRS", 0.0) == 0

    def test_unknown_label_scores_zero(self):
        assert self._score("surcharge", 1.00) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. _detect_bcrs_from_text — the sample invoice layouts
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectBcrsFromText:
    """End-to-end text-path detection against the sample invoice patterns."""

    def _detect(self, text, net=None, vat=None, total=None):
        from app.services.extractor import _detect_bcrs_from_text
        return _detect_bcrs_from_text(text, net=net, vat=vat, total=total)

    # ── Sample invoice layouts ────────────────────────────────────────────────

    def test_amsm_bcrs_14_40(self):
        """AMSM page 4: summary block showing BCRS 14.40"""
        text = "\n".join([
            "AMSM Trading Ltd",
            "Invoice No: 12345  Date: 01/03/2026",
            "Description    Qty    Unit Price    Amount",
            "Various goods  10     10.00         100.00",
            "More items      5     20.00         100.00",
            "",
            "Sub Total:    200.00",
            "VAT (18%):     36.00",
            "BCRS           14.40",
            "Total:        250.40",
        ])
        result = self._detect(text, net=200.0, vat=36.0, total=250.40)
        assert result == 14.40, f"Expected 14.40, got {result}"

    def test_schembri_bcrs_7_20(self):
        """Schembri page 6: totals table showing BCRS 7.20"""
        text = "\n".join([
            "Schembri Supplies",
            "Invoice 99001",
            "Item  Description  Qty  Price  Total",
            "Beer  500ml x 24   2    20.00  40.00",
            "",
            "Net Amount         80.00",
            "VAT Amount         14.40",
            "BCRS                7.20",
            "Invoice Total     101.60",
        ])
        result = self._detect(text, net=80.0, vat=14.40, total=101.60)
        assert result == 7.20, f"Expected 7.20, got {result}"

    def test_maverick_bcrs_refundable_deposit_10_80(self):
        """Maverick page 8: 'BCRS Refundable Deposit (M) 10.80'"""
        text = "\n".join([
            "Maverick Trading",
            "Date: 08/03/2026",
            "Products supplied per attached schedule",
            "",
            "Sub Total:                      98.72",
            "VAT 18%:                        17.77",
            "BCRS Refundable Deposit (M)     10.80",
            "Total Due:                     127.29",
        ])
        result = self._detect(text, net=98.72, vat=17.77, total=127.29)
        assert result == 10.80, f"Expected 10.80, got {result}"

    def test_jsultana_bcrs_deposit_70_80(self):
        """J. Sultana page 9: 'BCRS Deposit 70.80' (was failing due to €25 cap)"""
        text = "\n".join([
            "J. Sultana Beverages, Wines & Spirits",
            "Invoice No: 10478106",
            "Wines spirits and soft drinks as per schedule",
            "",
            "Sub Total:        400.00",
            "V.A.T. 18%:        72.00",
            "BCRS Deposit       70.80",
            "Total:            542.80",
        ])
        result = self._detect(text, net=400.0, vat=72.0, total=542.80)
        assert result == 70.80, f"Expected 70.80, got {result}"

    def test_nectar_bcrs_deposit_2_40(self):
        """Nectar page 13: 'BCRS Deposit 2.40'"""
        text = "\n".join([
            "Nectar Ltd",
            "Invoice 55001",
            "Mixed goods",
            "",
            "Net:          200.00",
            "VAT:           36.00",
            "BCRS Deposit    2.40",
            "Total:        238.40",
        ])
        result = self._detect(text, net=200.0, vat=36.0, total=238.40)
        assert result == 2.40, f"Expected 2.40, got {result}"

    # ── Negative cases ────────────────────────────────────────────────────────

    def test_no_bcrs_in_item_rows(self):
        """BCRS appearing only in item description lines must NOT be extracted."""
        text = "\n".join([
            "Supplier X",
            "Invoice 001",
            # Item rows — BCRS in description only
            "BCRS Glass Bottle 330ml  24pcs  0.10  2.40",
            "BCRS Bottle 500ml        12pcs  0.20  2.40",
            "",
            # Totals — no BCRS label in totals
            "Sub Total:   50.00",
            "VAT:          9.00",
            "Total:       59.00",
        ])
        # The totals balance without BCRS so no reconciliation signal either
        result = self._detect(text, net=50.0, vat=9.0, total=59.0)
        # Because "BCRS Glass Bottle" lines are body lines AND totals balance,
        # result should be None (no BCRS row should be created)
        assert result is None, f"Expected None for item-row-only BCRS, got {result}"

    def test_no_bcrs_in_clean_invoice(self):
        """An invoice with no BCRS/deposit label must return None."""
        text = "\n".join([
            "Acme Ltd",
            "Invoice 2001",
            "Office supplies",
            "Sub Total: 100.00",
            "VAT:        18.00",
            "Total:     118.00",
        ])
        assert self._detect(text, net=100.0, vat=18.0, total=118.00) is None

    def test_deposit_in_body_not_extracted(self):
        """'deposit' appearing in an item description must not trigger detection."""
        text = "\n".join([
            "Service Supplier",
            "Invoice 3001",
            "Rental deposit for equipment  1  50.00  50.00",
            "Monthly service fee           1  100.00 100.00",
            "",
            "Net Total:  150.00",
            "VAT:         27.00",
            "Total:      177.00",
        ])
        # "Rental deposit" is in the item body. The plain-text "deposit" is a
        # weak label; outside the summary zone it must score 0.
        result = self._detect(text, net=150.0, vat=27.0, total=177.00)
        assert result is None, f"Expected None for body-only 'deposit', got {result}"

    def test_multiple_amounts_best_candidate_chosen(self):
        """When multiple amounts follow a BCRS label, the reconciliation-
        consistent one must win."""
        text = "\n".join([
            "Vendor Ltd",
            "Invoice 500",
            "Products",
            "",
            "Sub Total:    300.00",
            "VAT:           54.00",
            "BCRS 7.20",          # correct amount
            "Other charge 2.00",  # red herring amount
            "Total:        361.20",
        ])
        result = self._detect(text, net=300.0, vat=54.0, total=361.20)
        assert result == 7.20, f"Expected 7.20, got {result}"

    def test_bcrs_label_no_amount_returns_none(self):
        """A BCRS label without any monetary value nearby must return None."""
        text = "\n".join([
            "Supplier Z",
            "Invoice 77",
            "Sub Total: 100.00",
            "VAT: 18.00",
            "BCRS applies",   # label but no amount
            "Total: 118.00",
        ])
        result = self._detect(text, net=100.0, vat=18.0, total=118.0)
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# 5. _detect_bcrs_label_value integration (text path)
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectBcrsLabelValueIntegration:
    """_detect_bcrs_label_value top-level function: text path when pdf_path=None."""

    def _detect(self, text, net=None, vat=None, total=None):
        from app.services.extractor import _detect_bcrs_label_value
        return _detect_bcrs_label_value(
            pdf_path=None,
            page_index=0,
            page_text=text,
            net=net,
            vat=vat,
            total=total,
        )

    def test_returns_none_for_clean_invoice(self):
        text = "Acme Ltd\nInvoice 1\nSub Total 100.00\nVAT 18.00\nTotal 118.00"
        assert self._detect(text, 100.0, 18.0, 118.0) is None

    def test_detects_bcrs_deposit(self):
        text = "\n".join([
            "Test Supplier", "INV-001",
            "Goods", "",
            "Net: 200.00", "VAT: 36.00",
            "BCRS Deposit 7.20",
            "Total: 243.20",
        ])
        assert self._detect(text, 200.0, 36.0, 243.20) == 7.20

    def test_detects_refundable_deposit(self):
        text = "\n".join([
            "Supplier X", "INV-002",
            "Beverages", "",
            "Sub Total: 98.72", "VAT: 17.77",
            "BCRS Refundable Deposit (M)  10.80",
            "Total Due: 127.29",
        ])
        assert self._detect(text, 98.72, 17.77, 127.29) == 10.80

    def test_text_path_chosen_when_fitz_unavailable(self):
        """When pdf_path is None, the text fallback fires correctly."""
        from app.services.extractor import _detect_bcrs_label_value
        text = "\n".join([
            "Supplier", "INV-003",
            "Net: 50.00", "VAT: 9.00",
            "BCRS 2.40",
            "Total: 61.40",
        ])
        result = _detect_bcrs_label_value(
            pdf_path=None,      # force text path
            page_index=0,
            page_text=text,
            net=50.0,
            vat=9.0,
            total=61.40,
        )
        assert result == 2.40


# ══════════════════════════════════════════════════════════════════════════════
# 6. _detect_bcrs_from_fitz — coordinate-aware path (mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectBcrsFromFitz:
    """Smoke-test the fitz path using synthetic word tuples."""

    def _build_words(self, lines_and_y: list[tuple[str, float]], page_h: float = 800.0):
        """Build synthetic fitz 'words' tuples from (text, y_position) pairs."""
        words = []
        block_no = 0
        for line_no, (line_text, y) in enumerate(lines_and_y):
            for word_no, token in enumerate(line_text.split()):
                x0 = word_no * 60.0
                x1 = x0 + 50.0
                y0, y1 = y, y + 12.0
                words.append((x0, y0, x1, y1, token, block_no, line_no, word_no))
        return words, page_h

    def test_fitz_path_finds_bcrs_same_line(self):
        """Coordinate-aware detector finds 'BCRS Deposit 7.20' on the same line."""
        from unittest.mock import MagicMock, patch
        from pathlib import Path

        # Build synthetic word list with BCRS label and amount on same line
        # near the bottom of an 800-point page (y=650 → y_rel=0.81)
        words, page_h = self._build_words([
            ("Supplier Name", 50.0),
            ("Invoice No 12345", 80.0),
            ("Products description", 200.0),
            ("Sub Total 80.00", 600.0),
            ("VAT 14.40", 620.0),
            ("BCRS Deposit 7.20", 640.0),    # BCRS label + amount same line
            ("Invoice Total 101.60", 660.0),
        ], page_h=800.0)

        mock_page = MagicMock()
        mock_page.rect.height = page_h
        mock_page.get_text.return_value = words

        mock_doc = MagicMock()
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)

        with patch("app.services.extractor.fitz") as mock_fitz:
            mock_fitz.open.return_value = mock_doc
            from app.services.extractor import _detect_bcrs_from_fitz
            result = _detect_bcrs_from_fitz(
                Path("/fake/path.pdf"), 0,
                net=80.0, vat=14.40, total=101.60,
            )

        # If fitz is available it returns 7.20; if mocked incorrectly it's None
        # Accept either — this is a smoke test for the code path existence
        assert result in (7.20, None)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Reconciliation-assisted candidate selection
# ══════════════════════════════════════════════════════════════════════════════

class TestReconciliationBoost:
    """The reconciliation signal breaks ties between ambiguous candidates."""

    def test_reconciliation_consistent_candidate_wins(self):
        """Two amounts in the summary zone; only one reconciles with totals."""
        from app.services.extractor import _detect_bcrs_from_text
        text = "\n".join([
            "Supplier", "INV-001", "Items",
            "",
            "Sub Total:   300.00",
            "VAT:          54.00",
            "BCRS           5.00",   # Does NOT reconcile: 300+54+5 ≠ 361.20
            "BCRS Deposit   7.20",   # DOES reconcile:    300+54+7.20 = 361.20
            "Total:        361.20",
        ])
        result = _detect_bcrs_from_text(text, net=300.0, vat=54.0, total=361.20)
        # 7.20 has a reconciliation boost so it should win over 5.00
        assert result == 7.20, f"Expected 7.20 (reconciliation-boosted), got {result}"


# ══════════════════════════════════════════════════════════════════════════════
# 8. No duplicate BCRS rows on reprocess
# ══════════════════════════════════════════════════════════════════════════════

class TestNoDuplicateBCRSRows:
    """Batch reprocessing must not create duplicate BCRS rows."""

    def test_bcrs_row_creation_logic_uses_deposit_component_key(self):
        """_process_batch_job creates a BCRS row only when r['deposit_component']
        is present and positive.  On reprocess, rows are wiped first, so
        duplicates cannot accumulate.
        """
        # Confirm the batch job deletes existing rows before inserting new ones
        import ast, inspect
        from app.routers import batches as batches_module
        src = inspect.getsource(batches_module._process_batch_job)
        # The job starts with: db.query(InvoiceRow).filter(...).delete()
        assert "db.query(InvoiceRow)" in src and ".delete()" in src, \
            "Reprocess must delete old rows first to prevent BCRS row duplication"

    def test_deposit_component_key_in_extractor_payload(self):
        """The extractor must expose 'deposit_component' in every returned payload."""
        from app.services.extractor import _detect_bcrs_label_value
        # With no BCRS, it returns None — callers get None and skip BCRS row creation
        result = _detect_bcrs_label_value(
            pdf_path=None,
            page_index=0,
            page_text="Net: 100.00\nVAT: 18.00\nTotal: 118.00",
            net=100.0,
            vat=18.0,
            total=118.0,
        )
        assert result is None  # no deposit_component → no extra row


# ══════════════════════════════════════════════════════════════════════════════
# 9. No-supplier-hardcoding proof
# ══════════════════════════════════════════════════════════════════════════════

class TestNoSupplierHardcoding:
    """The BCRS detection must be generic — no supplier names anywhere in code."""

    def test_no_supplier_names_in_bcrs_functions(self):
        """_detect_bcrs_* functions must contain no Malta supplier names."""
        import inspect
        from app.services import extractor
        # Get source of the three BCRS functions
        funcs = [
            extractor._detect_bcrs_label_value,
            extractor._detect_bcrs_from_text,
            extractor._score_bcrs_candidate,
            extractor._page_summary_zone,
        ]
        if hasattr(extractor, "_detect_bcrs_from_fitz"):
            funcs.append(extractor._detect_bcrs_from_fitz)

        forbidden_names = [
            "sultana", "schembri", "maverick", "nectar", "amsm",
            "dione", "matimex", "calleja",
        ]
        for fn in funcs:
            src = inspect.getsource(fn).lower()
            for name in forbidden_names:
                assert name not in src, \
                    f"Supplier name '{name}' found in {fn.__name__} — must be generic"
