from __future__ import annotations

import io
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import requests

from app.config import settings
from app.services.ocr import OCRBackend

logger = logging.getLogger(__name__)


def _parse_orientation_angle(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        raw = float(m.group(0))
    except Exception:
        return None
    normalised = int(round(raw / 90.0) * 90) % 360
    return normalised if normalised in {0, 90, 180, 270} else None


def _as_correction_from_content_angle(value: Any) -> int | None:
    """Convert a detected content angle into a clockwise correction angle."""
    angle = _parse_orientation_angle(value)
    if angle is None:
        return None
    return (360 - angle) % 360


def _detect_with_azure_di(jpeg_bytes: bytes, page_no: int) -> int | None:
    """Use Azure Document Intelligence page.angle as orientation signal."""
    if not (settings.use_azure_di and settings.azure_di_endpoint and settings.azure_di_key):
        return None
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential
        import concurrent.futures as _cf

        client = DocumentIntelligenceClient(
            endpoint=settings.azure_di_endpoint.rstrip("/"),
            credential=AzureKeyCredential(settings.azure_di_key),
        )
        poller = client.begin_analyze_document(
            "prebuilt-read",
            body=jpeg_bytes,
            content_type="image/jpeg",
            polling_interval=1,
            connection_timeout=min(float(getattr(settings, "azure_di_page_timeout_s", 25)), 20.0),
            read_timeout=min(float(getattr(settings, "azure_di_page_timeout_s", 25)), 20.0),
        )
        timeout_s = min(float(getattr(settings, "azure_di_page_timeout_s", 25)), 25.0)
        pool = _cf.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(poller.result)
        try:
            result = fut.result(timeout=timeout_s)
        except _cf.TimeoutError:
            fut.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            logger.warning("Azure DI orientation timeout after %.0fs on page %d", timeout_s, page_no)
            return None
        finally:
            if fut.done():
                pool.shutdown(wait=False, cancel_futures=True)
        pages = getattr(result, "pages", None) or []
        if pages:
            raw_angle = getattr(pages[0], "angle", None)
            correction = _as_correction_from_content_angle(raw_angle)
            if correction is not None:
                logger.info(
                    "Azure DI orientation page %d: content_angle=%s correction=%s",
                    page_no, raw_angle, correction,
                )
                return correction
    except Exception as exc:
        logger.debug("Azure DI orientation detection skipped for page %d: %s", page_no, exc)
    return None


def _detect_with_tesseract(jpeg_bytes: bytes) -> int | None:
    if not shutil.which("tesseract"):
        return None
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore

        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        osd = pytesseract.image_to_osd(img) or ""
        angle = None
        confidence = 0.0
        for line in osd.splitlines():
            if line.lower().startswith("rotate:"):
                angle = _parse_orientation_angle(line)
            elif line.lower().startswith("orientation confidence:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except Exception:
                    confidence = 0.0
        if angle is not None and (angle == 0 or confidence >= 1.0):
            return angle
    except Exception as exc:
        logger.debug("Tesseract orientation detection skipped: %s", exc)
    return None


def _walk_json_values(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk_json_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_values(item)


def _detect_with_ocr_space(jpeg_bytes: bytes, page_no: int) -> int | None:
    if not settings.ocr_space_api_key:
        return None
    try:
        resp = requests.post(
            settings.ocr_space_endpoint,
            files={"file": (f"orientation_page_{page_no}.jpg", jpeg_bytes, "image/jpeg")},
            data={
                "apikey": settings.ocr_space_api_key,
                "language": settings.ocr_space_language,
                "isOverlayRequired": "false",
                "detectOrientation": "true",
                "scale": str(settings.ocr_space_scale).lower(),
                "OCREngine": str(settings.ocr_space_ocr_engine),
            },
            timeout=min(float(settings.ocr_space_timeout_seconds or 30), 30.0),
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("IsErroredOnProcessing"):
            return None
        priority_keys = {"textorientation", "orientation", "rotate", "rotation"}
        for key, value in _walk_json_values(payload):
            if str(key).replace("_", "").lower() in priority_keys:
                angle = _parse_orientation_angle(value)
                if angle is not None:
                    return angle
    except Exception as exc:
        logger.debug("OCR.space orientation detection skipped for page %d: %s", page_no, exc)
    return None


def _detect_page_rotation(jpeg_bytes: bytes, page_no: int) -> int:
    """Return clockwise rotation needed to make the rendered page upright."""
    angle = _detect_with_azure_di(jpeg_bytes, page_no)
    if angle is not None:
        return angle
    angle = _detect_with_tesseract(jpeg_bytes)
    if angle is not None:
        return angle
    angle = _detect_with_ocr_space(jpeg_bytes, page_no)
    if angle is not None:
        return angle
    return 0


def normalise_pdf_orientation(pdf_path: str | Path) -> tuple[Path, dict[int, int]]:
    """Create an upright working PDF when rendered pages are rotated.

    The returned mapping is 1-based page number -> clockwise rotation applied.
    If no correction is needed, the original path is returned unchanged.
    """
    src = Path(pdf_path)
    if not bool(getattr(settings, "normalize_page_orientation", True)):
        return src, {}
    if not src.exists():
        return src, {}

    rotations: dict[int, int] = {}
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(src))
        try:
            page_count = len(doc)
            for idx in range(page_count):
                page_no = idx + 1
                try:
                    jpeg = OCRBackend.render_pdf_page_to_jpeg_bytes(src, idx, scale=1.0, quality=65)
                    detected = _detect_page_rotation(jpeg, page_no)
                except Exception as exc:
                    logger.debug("Orientation render/detect failed page %d: %s", page_no, exc)
                    detected = 0
                if detected in {90, 180, 270}:
                    page = doc[idx]
                    page.set_rotation((int(page.rotation or 0) + detected) % 360)
                    rotations[page_no] = detected
            if not rotations:
                return src, {}
            out = src.with_name(f"{src.stem}.oriented.pdf")
            if out.exists():
                try:
                    out.unlink()
                except Exception:
                    pass
            doc.save(str(out), garbage=4, deflate=True)
            logger.info("Normalised PDF orientation for %s: %s", src.name, rotations)
            return out, rotations
        finally:
            doc.close()
    except Exception as exc:
        logger.warning("PDF orientation normalisation failed for %s: %s", src, exc)
        return src, {}
