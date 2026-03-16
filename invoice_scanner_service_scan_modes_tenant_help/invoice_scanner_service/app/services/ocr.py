from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import requests

from app.config import settings

logger = logging.getLogger("invoice_scanner.ocr")


class OCRBackend:
    name = "base"

    def extract_text_from_pdf_page(self, pdf_path: Path, page_index: int, scale: float = 1.8) -> str:
        raise NotImplementedError

    @staticmethod
    def _render_pdf_page(pdf_path: Path, page_index: int, scale: float = 1.8, quality: int = 65) -> bytes:
        pdf = pdfium.PdfDocument(str(pdf_path))
        page = None
        image = None
        try:
            page = pdf.get_page(page_index)
            image = page.render(scale=scale).to_pil()
            buf = BytesIO()
            image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
        finally:
            try:
                if image is not None:
                    image.close()
            except Exception:
                pass
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            try:
                pdf.close()
            except Exception:
                pass

    @classmethod
    def render_pdf_page_to_jpeg_bytes(
        cls,
        pdf_path: Path,
        page_index: int,
        scale: float = 1.8,
        quality: int = 65,
    ) -> bytes:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cls._render_pdf_page, pdf_path, page_index, scale, quality)
            try:
                return future.result(timeout=settings.page_render_timeout_seconds)
            except FuturesTimeoutError as exc:
                logger.warning(
                    "PDF page render timed out",
                    extra={"file_name": pdf_path.name, "page_no": page_index + 1, "stage": "pdf_render", "status": "timeout"},
                )
                raise TimeoutError(f"PDF page render exceeded {settings.page_render_timeout_seconds} seconds") from exc


class OCRSpaceBackend(OCRBackend):
    name = "ocr_space"

    def __init__(self) -> None:
        if not settings.ocr_space_api_key:
            raise RuntimeError("OCR.space API key is missing. Set OCR_SPACE_API_KEY.")

    def extract_text_from_pdf_page(self, pdf_path: Path, page_index: int, scale: float = 1.8) -> str:
        image_bytes = self.render_pdf_page_to_jpeg_bytes(
            pdf_path,
            page_index,
            scale=scale,
            quality=65,
        )
        if not image_bytes:
            return ""

        files = {
            "file": (f"page_{page_index + 1}.jpg", image_bytes, "image/jpeg")
        }

        data = {
            "apikey": settings.ocr_space_api_key,
            "language": settings.ocr_space_language,
            "isOverlayRequired": str(settings.ocr_space_overlay_required).lower(),
            "scale": str(settings.ocr_space_scale).lower(),
            "OCREngine": str(settings.ocr_space_ocr_engine),
        }

        logger.info(
            "OCR request started",
            extra={"file_name": pdf_path.name, "page_no": page_index + 1, "stage": "ocr", "status": "started"},
        )
        resp = requests.post(
            settings.ocr_space_endpoint,
            files=files,
            data=data,
            timeout=settings.ocr_space_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("IsErroredOnProcessing"):
            msg = payload.get("ErrorMessage") or payload.get("ErrorDetails") or "OCR.space processing error"
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            raise RuntimeError(str(msg))

        lines: list[str] = []
        for item in payload.get("ParsedResults") or []:
            text = (item or {}).get("ParsedText")
            if text:
                lines.append(text)

        logger.info(
            "OCR request completed",
            extra={"file_name": pdf_path.name, "page_no": page_index + 1, "stage": "ocr", "status": "ok"},
        )
        return "\n".join(lines).strip()


class PaddleOCRBackend(OCRBackend):
    name = "paddleocr"

    def __init__(self) -> None:
        raise RuntimeError("PaddleOCR disabled for this deployment. Use OCR_PROVIDER=ocr_space.")
