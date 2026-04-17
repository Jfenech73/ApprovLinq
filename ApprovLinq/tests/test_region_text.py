"""
Regression tests for _read_region_text 3-tier fallback.

Root cause fixed:
  _read_region_text used a bare `if text:` guard after PyMuPDF get_textbox().
  On Dione invoices (bad/sparse text layer) get_textbox returns stray whitespace
  or non-printable artefacts that pass `if text:` but are useless.  The function
  returned that garbage and never reached the OCR fallback.

Fix:
  Tier 1 (fitz get_textbox)           — accepted only if >= 2 alphanumeric chars
  Tier 2 (pypdfium2 get_text_bounded) — independent parser, different coord system
  Tier 3 (cropped-region OCR.space)   — last resort, renders only the selected crop

Run: pytest tests/test_region_text.py -v
"""
from __future__ import annotations

import ast, os, re, sys, types, unittest.mock as mock


def _src(f: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()


def _load_read_region():
    """Load _count_meaningful and _read_region_text from live review.py source."""
    src = _src("app/routers/review.py")
    start = src.find("def _count_meaningful")
    end   = src.find("\n\n# ── Remap hints")
    # Minimal execution context
    import io as _io
    import logging as _logging
    ns: dict = {
        "io": _io,
        "logger": _logging.getLogger("test_region"),
        "settings": types.SimpleNamespace(
            ocr_space_api_key=None,
            ocr_space_endpoint="",
            ocr_space_language="auto",
            ocr_space_ocr_engine=2,
            ocr_space_timeout_seconds=30,
        ),
    }
    exec(src[start:end], ns)
    return ns["_count_meaningful"], ns["_read_region_text"]


_count_meaningful, _read_region_text = _load_read_region()


# ══════════════════════════════════════════════════════════════════════════════
# 1.  _count_meaningful
# ══════════════════════════════════════════════════════════════════════════════

class TestCountMeaningful:
    def test_alphanumeric_counted(self):
        assert _count_meaningful("INV-45005") == 8   # I,N,V,4,5,0,0,5

    def test_whitespace_only_is_zero(self):
        assert _count_meaningful("   \t\n") == 0

    def test_empty_is_zero(self):
        assert _count_meaningful("") == 0

    def test_none_is_zero(self):
        assert _count_meaningful(None) == 0

    def test_punctuation_only_is_zero(self):
        assert _count_meaningful("---///...") == 0

    def test_mixed(self):
        assert _count_meaningful("€45.00") == 4   # 4,5,0,0


# ══════════════════════════════════════════════════════════════════════════════
# 2.  Tier 1 gate — sparse fitz result must not block fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestTier1Gate:
    """If fitz get_textbox returns junk/sparse text, tier2 must be attempted."""

    def test_tier1_accepted_when_meaningful(self):
        """Tier 1 returns real text → use it, no tier 2 needed."""
        fake_page  = types.SimpleNamespace(
            rect=types.SimpleNamespace(width=595, height=842),
            get_textbox=lambda r: "INV-45005",
        )
        fake_doc = types.SimpleNamespace(
            page_count=1,
            load_page=lambda i: fake_page,
            close=lambda: None,
        )
        fake_doc.__enter__ = lambda s: s
        fake_doc.__exit__ = lambda s, *a: None

        import app.routers.review as rv_mod
        with mock.patch("fitz.open", return_value=fake_doc):
            result = rv_mod._read_region_text("fake.pdf", 1, 0.1, 0.1, 0.3, 0.05)
        assert result == "INV-45005"

    def test_tier1_rejected_when_sparse_falls_through(self):
        """If fitz returns only whitespace/junk (0 meaningful chars), tier2 runs."""
        fake_page = types.SimpleNamespace(
            rect=types.SimpleNamespace(width=595, height=842),
            get_textbox=lambda r: "   \x00  ",  # looks truthy but useless
        )
        fake_doc = types.SimpleNamespace(
            page_count=1,
            load_page=lambda i: fake_page,
            close=lambda: None,
        )

        import app.routers.review as rv_mod
        tier2_called = []

        def mock_fitz_open(path):
            return fake_doc

        # Patch tier 2 to record call and return good text
        original_fn = rv_mod._read_region_text

        # We can't easily mock tier2 internals without monkey-patching pypdfium2,
        # so we test at the structural level instead.
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]

        # The old bug was `if text:` — new code must gate on _count_meaningful
        assert "if t1:" not in fn, "Old bug present: bare 'if t1:' check"
        assert "if text:" not in fn, "Old bug present: bare 'if text:' check"
        assert "_count_meaningful(t1)" in fn, "tier1 must call _count_meaningful(t1)"

    def test_tier1_rejected_when_exactly_1_meaningful_char(self):
        """Single meaningful char (e.g. 'A') is not enough — tier2 must run."""
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        tier1_block = fn[fn.find("tier1"):fn.find("tier2")]
        # The gate must be >= 2, not >= 1
        assert "m1 >= 2" in tier1_block or ">= 2" in tier1_block, \
            "tier1 gate must require >= 2 meaningful chars"


# ══════════════════════════════════════════════════════════════════════════════
# 3.  Tier 2 — pypdfium2 coordinate conversion
# ══════════════════════════════════════════════════════════════════════════════

class TestTier2CoordinateConversion:
    """pypdfium2 uses PDF coordinate system (y=0 at bottom).
    The stored normalised coords are image-style (y=0 at top).
    Tier 2 must flip the y-axis."""

    def test_y_axis_flip_present_in_code(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        tier2_block = fn[fn.find("tier2"):fn.find("tier3")]
        assert "1.0 - " in tier2_block, \
            "tier2 must flip y-coordinate: bottom = (1.0 - (y + h)) * ph"

    def test_get_text_bounded_called_in_tier2(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        assert "get_text_bounded" in fn[fn.find("tier2"):fn.find("tier3")]

    def test_get_textpage_called_in_tier2(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        assert "get_textpage" in fn[fn.find("tier2"):fn.find("tier3")]


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Tier 3 — cropped OCR, not whole-page OCR
# ══════════════════════════════════════════════════════════════════════════════

class TestTier3CroppedOCR:
    def test_crop_applied_before_ocr(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        tier3_block = fn[fn.find("tier3"):]
        render_idx = tier3_block.find("render(scale=")
        crop_idx   = tier3_block.find(".crop(")
        ocr_idx    = tier3_block.find("ocr_space_api_key")
        assert render_idx < crop_idx < ocr_idx, \
            "tier3 must render → crop → OCR in that order"

    def test_no_whole_page_ocr_without_crop(self):
        """The whole-page render result must be cropped before sending to OCR."""
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        tier3_block = fn[fn.find("tier3"):]
        # The img_bytes sent to OCR must come from the cropped image, not full render
        assert ".crop(" in tier3_block
        # img_bytes (or _img_bytes) must be assigned from the crop buffer
        assert "crop.save(" in tier3_block or "_crop.save(" in tier3_block

    def test_tier3_skipped_when_ocr_not_configured(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]
        tier3_block = fn[fn.find("tier3"):]
        assert "ocr_space_api_key" in tier3_block, \
            "tier3 must check for OCR.space API key before attempting OCR"


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Resolution order: selected_text > _read_region_text
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveRemapPriorityOrder:
    def test_selected_text_checked_before_read_region(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def save_remap"):src.find("def save_remap") + 4000]
        sel_idx = fn.find("selected_text")
        rrt_idx = fn.find("_read_region_text(")
        assert sel_idx > 0, "selected_text must be handled in save_remap"
        assert rrt_idx > 0, "_read_region_text must be called in save_remap"
        assert sel_idx < rrt_idx, \
            "selected_text must be checked BEFORE falling back to _read_region_text"

    def test_selected_text_in_remapin_schema(self):
        src = _src("app/routers/review.py")
        model = src[src.find("class RemapIn"):src.find("\ndef _get_batch")]
        assert "selected_text" in model


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Logging
# ══════════════════════════════════════════════════════════════════════════════

class TestLogging:
    def _fn(self) -> str:
        src = _src("app/routers/review.py")
        return src[src.find("def _read_region_text"):src.find("\n\n# ── Remap hints")]

    def test_entry_log_present(self):
        """Must log file/page/region on entry."""
        fn = self._fn()
        assert "logger.debug(" in fn
        assert "file_path" in fn[fn.find("logger.debug("):fn.find("logger.debug(")+200] or \
               "file=" in fn[fn.find("logger.debug("):fn.find("logger.debug(")+200]

    def test_tier1_result_logged(self):
        fn = self._fn()
        assert "tier1" in fn[fn.find("logger.debug"):fn.find("tier2")]

    def test_tier2_result_logged(self):
        fn = self._fn()
        assert "tier2" in fn[fn.find("logger.debug"):fn.find("tier3")]

    def test_tier3_fallback_logged(self):
        fn = self._fn()
        tier3_idx = fn.find("tier3")
        assert "logger.debug(" in fn[tier3_idx:], "tier3 must have at least one debug log"

    def test_meaningful_char_count_logged(self):
        fn = self._fn()
        assert "meaningful=" in fn or "meaningful=%d" in fn, \
            "meaningful char count must appear in log output"


# ══════════════════════════════════════════════════════════════════════════════
# 7.  _count_meaningful as standalone helper (not inlined)
# ══════════════════════════════════════════════════════════════════════════════

class TestCountMeaningfulHelper:
    def test_defined_before_read_region_text(self):
        src = _src("app/routers/review.py")
        cm_idx  = src.find("def _count_meaningful")
        rrt_idx = src.find("def _read_region_text")
        assert cm_idx > 0,  "_count_meaningful must be defined"
        assert cm_idx < rrt_idx, "_count_meaningful must be defined before _read_region_text"

    def test_callable_in_isolation(self):
        assert _count_meaningful("hello123") == 8
        assert _count_meaningful("") == 0


# ══════════════════════════════════════════════════════════════════════════════
# 8.  Stable logic untouched
# ══════════════════════════════════════════════════════════════════════════════

class TestStableLogic:
    def test_bcrs_unchanged(self):
        src = _src("app/routers/batches.py")
        bcrs = src[src.find("def _extract_bcrs_amount_from_summary"):
                   src.find("\ndef _build_bcrs_row")]
        assert "has_label_line" in bcrs
        assert "best_score < 20" in bcrs
        assert "_is_vat_line" in bcrs

    def test_split_math_unchanged(self):
        src = _src("app/routers/batches.py")
        assert "_corrected_total = round(_net + _vat, 2)" in src

    def test_save_remap_logic_preserved(self):
        src = _src("app/routers/review.py")
        assert "RemapHint" in src
        assert "CorrectionRule" in src
        assert "remap_field_value" in src
        assert "get_or_create_correction" in src

    def test_all_files_parse(self):
        for f in ["app/routers/review.py", "app/routers/batches.py",
                  "app/services/extractor.py"]:
            ast.parse(_src(f))
