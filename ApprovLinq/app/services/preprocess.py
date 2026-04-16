"""Page image preprocessing for improved OCR/AI extraction quality.

Applies contrast enhancement, sharpness boost, bleed-through suppression,
and computes a page quality score.  This module supersedes the
``preprocess_page_image`` helper that was previously embedded inside
``extractor.py``.

Public API
──────────
    preprocess_page(jpeg_bytes: bytes) -> PageImage
    score_page_quality(jpeg_bytes: bytes) -> float

PageImage fields:
    processed_bytes  - preprocessed JPEG bytes
    quality_score    - 0.0 (unusable) to 1.0 (perfect scan)
    contrast_score   - raw contrast component (std-dev of grayscale pixels / 75)
    brightness_score - raw brightness component (closeness to mid-grey)
    mean_brightness  - mean grayscale pixel value 0–255
    width, height    - image dimensions in pixels
"""
from __future__ import annotations

import io
import logging
import statistics
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PageImage:
    processed_bytes: bytes
    quality_score: float
    contrast_score: float
    brightness_score: float
    mean_brightness: float
    width: int
    height: int


def preprocess_page(jpeg_bytes: bytes) -> PageImage:
    """Preprocess a page JPEG for better OCR/AI extraction.

    Steps applied:
        1. Contrast boost × 1.45  (helps faded/low-ink scans)
        2. Sharpness boost × 1.25 (helps blurry or lo-res scans)
        3. Median filter 3×3      (bleed-through / noise suppression)
        4. Brightness lift × 1.20 (only for very dark pages, mean < 100)

    Quality score:
        contrast_score  = min(pixel_std_dev / 75, 1.0)
        brightness_score = 1.0 − |mean − 128| / 128
        quality_score   = 0.65 × contrast + 0.35 × brightness
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter  # type: ignore

        img = Image.open(io.BytesIO(jpeg_bytes))

        # Ensure RGB mode
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        width, height = img.size

        # ── Quality scoring on raw (unenhanced) grayscale ─────────────────
        gray = img.convert("L")
        pixels = list(gray.getdata())
        mean_px = sum(pixels) / max(len(pixels), 1)
        try:
            std_px = statistics.stdev(pixels)
        except statistics.StatisticsError:
            std_px = 0.0

        contrast_score  = min(std_px / 75.0, 1.0)
        brightness_score = 1.0 - abs(mean_px - 128.0) / 128.0
        quality_score   = round(contrast_score * 0.65 + brightness_score * 0.35, 3)

        # ── Enhancement pipeline ──────────────────────────────────────────
        img = ImageEnhance.Contrast(img).enhance(1.45)
        img = ImageEnhance.Sharpness(img).enhance(1.25)
        img = img.filter(ImageFilter.MedianFilter(size=3))

        if mean_px < 100:
            img = ImageEnhance.Brightness(img).enhance(1.20)

        # ── Output ────────────────────────────────────────────────────────
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        processed = buf.getvalue()

        logger.debug(
            "preprocess_page: quality=%.3f contrast=%.3f brightness=%.3f "
            "size=%dx%d in=%dB out=%dB",
            quality_score, contrast_score, brightness_score,
            width, height, len(jpeg_bytes), len(processed),
        )

        return PageImage(
            processed_bytes=processed,
            quality_score=quality_score,
            contrast_score=round(contrast_score, 3),
            brightness_score=round(brightness_score, 3),
            mean_brightness=round(mean_px, 1),
            width=width,
            height=height,
        )

    except Exception as exc:
        logger.warning("preprocess_page failed (returning original): %s", exc)
        return PageImage(
            processed_bytes=jpeg_bytes,
            quality_score=0.5,
            contrast_score=0.5,
            brightness_score=0.5,
            mean_brightness=128.0,
            width=0,
            height=0,
        )


def score_page_quality(jpeg_bytes: bytes) -> float:
    """Return a quality score 0.0–1.0 without applying any enhancements."""
    try:
        from PIL import Image  # type: ignore
        import statistics as _stats

        img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
        pixels = list(img.getdata())
        mean_px = sum(pixels) / max(len(pixels), 1)
        try:
            std_px = _stats.stdev(pixels)
        except _stats.StatisticsError:
            std_px = 0.0
        contrast_score  = min(std_px / 75.0, 1.0)
        brightness_score = 1.0 - abs(mean_px - 128.0) / 128.0
        return round(contrast_score * 0.65 + brightness_score * 0.35, 3)
    except Exception:
        return 0.5
