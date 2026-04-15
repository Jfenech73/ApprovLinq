from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import requests

from app.config import settings


class OCRBackend:
    name = "base"

    def extract_text_from_pdf_page(self, pdf_path: Path, page_index: int, scale: float = 1.8) -> str:
        raise NotImplementedError

    @staticmethod
    def render_pdf_page_to_jpeg_bytes(
        pdf_path: Path,
        page_index: int,
        scale: float = 1.8,
        quality: int = 65,
    ) -> bytes:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page = pdf.get_page(page_index)
            try:
                image = page.render(scale=scale).to_pil()
                buf = BytesIO()
                image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
                return buf.getvalue()
            finally:
                page.close()
        finally:
            pdf.close()


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

        return "\n".join(lines).strip()


class PaddleOCRBackend(OCRBackend):
    name = "paddleocr"

    def __init__(self) -> None:
        raise RuntimeError("PaddleOCR disabled for this deployment. Use OCR_PROVIDER=ocr_space.")