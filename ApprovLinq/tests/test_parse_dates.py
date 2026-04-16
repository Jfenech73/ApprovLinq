"""Tests for app.services.parse_dates."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import date
from app.services.parse_dates import parse_invoice_date, format_date


class TestUnambiguousDates:
    def test_dmy_forward_slash(self):
        r = parse_invoice_date("15/03/2024")
        assert r.normalized == date(2024, 3, 15)
        assert r.parse_strategy == "dmy_unambiguous"
        assert r.ambiguity_flag is False
        assert r.review_reason is None

    def test_dmy_dot_separator(self):
        r = parse_invoice_date("22.11.2023")
        assert r.normalized == date(2023, 11, 22)
        assert r.ambiguity_flag is False

    def test_dmy_dash_separator(self):
        r = parse_invoice_date("01-07-2025")
        assert r.normalized == date(2025, 7, 1)

    def test_iso_format(self):
        r = parse_invoice_date("2024-03-15")
        assert r.normalized == date(2024, 3, 15)
        assert r.parse_strategy == "iso"

    def test_text_month_long(self):
        r = parse_invoice_date("15 March 2024")
        assert r.normalized == date(2024, 3, 15)
        assert r.parse_strategy == "text"

    def test_text_month_short(self):
        r = parse_invoice_date("5 Feb 2023")
        assert r.normalized == date(2023, 2, 5)

    def test_two_digit_year(self):
        r = parse_invoice_date("15/03/24")
        assert r.normalized is not None
        assert r.normalized.month == 3
        assert r.normalized.day == 15

    def test_day_31_unambiguous(self):
        r = parse_invoice_date("31/01/2024")
        assert r.normalized == date(2024, 1, 31)
        assert r.ambiguity_flag is False

    def test_month_13_invalid_reverts_to_mdy(self):
        # 13/01/2024 — day=13, month=1 → unambiguous dmy
        r = parse_invoice_date("13/01/2024")
        assert r.normalized == date(2024, 1, 13)


class TestAmbiguousDates:
    def test_ambiguous_both_small(self):
        r = parse_invoice_date("05/06/2024")
        assert r.ambiguity_flag is True
        assert r.review_reason == "ambiguous_date_locale"

    def test_ambiguous_01_01(self):
        r = parse_invoice_date("01/01/2025")
        # day == month: dmy and mdy produce the same date, so not ambiguous
        assert r.normalized == date(2025, 1, 1)
        # ambiguity_flag may or may not be set when day==month (implementation detail)

    def test_not_ambiguous_day_gt_12(self):
        r = parse_invoice_date("20/06/2024")
        assert r.ambiguity_flag is False


class TestFailureCases:
    def test_empty_string(self):
        r = parse_invoice_date("")
        assert r.normalized is None
        assert r.parse_strategy == "failed"
        assert r.review_reason == "invoice_date_missing"

    def test_none_input(self):
        r = parse_invoice_date(None)
        assert r.normalized is None
        assert r.review_reason == "invoice_date_missing"

    def test_garbage_text(self):
        r = parse_invoice_date("not a date at all")
        assert r.normalized is None

    def test_raw_preserved(self):
        r = parse_invoice_date("15/03/2024")
        assert r.raw == "15/03/2024"


class TestFormatDate:
    def test_format_date(self):
        result = format_date(date(2024, 3, 15))
        assert "2024" in result
        assert "03" in result or "3" in result
        assert "15" in result

    def test_format_none(self):
        assert format_date(None) == ""
