from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import requests

from app.config import settings


class OCRBackend:
    name = "base"

    def extract_text_from_pdf_page(self, pdf_path: Path, page_index: int, scale: float = 3.5) -> str:
        raise NotImplementedError

    @staticmethod
    def render_pdf_page_to_png_bytes(pdf_path: Path, page_index: int, scale: float = 3.5) -> bytes:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page = pdf.get_page(page_index)
            try:
                image = page.render(scale=scale).to_pil()
                buf = BytesIO()
                image.convert("RGB").save(buf, format="PNG")
                return buf.getvalue()
            finally:
                page.close()
        finally:
            pdf.close()


class OCRSpaceBackend(OCRBackend):
    name = "ocr_space"

    def __init__(self) -> None:
        provider = (settings.ocr_provider or "").strip().lower()
        if provider != "ocr_space":
            raise RuntimeError(
                f"OCRSpaceBackend selected while OCR_PROVIDER is '{settings.ocr_provider}'. "
                "Set OCR_PROVIDER=ocr_space."
            )
        if not settings.ocr_space_api_key:
            raise RuntimeError(
                "OCR.space API key is missing. "
                "Set OCR_SPACE_API_KEY in Koyeb environment variables."
            )

    def extract_text_from_pdf_page(self, pdf_path: Path, page_index: int, scale: float = 3.5) -> str:
        image_bytes = self.render_pdf_page_to_png_bytes(pdf_path, page_index, scale=scale)

        if not image_bytes:
            return ""

        files = {
            "file": (f"page_{page_index + 1}.png", image_bytes, "image/png")
        }

        data = {
            "apikey": settings.ocr_space_api_key,
            "language": settings.ocr_space_language,
            "isOverlayRequired": str(settings.ocr_space_overlay_required).lower(),
            "scale": str(settings.ocr_space_scale).lower(),
            "OCREngine": str(settings.ocr_space_ocr_engine),
        }

        try:
            resp = requests.post(
                settings.ocr_space_endpoint,
                files=files,
                data=data,
                timeout=settings.ocr_space_timeout_seconds,
            )
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"OCR.space request failed. "
                f"endpoint={settings.ocr_space_endpoint}, "
                f"provider={settings.ocr_provider}, "
                f"api_key_present={bool(settings.ocr_space_api_key)}, "
                f"language={settings.ocr_space_language}, "
                f"engine={settings.ocr_space_ocr_engine}, "
                f"timeout={settings.ocr_space_timeout_seconds}. "
                f"Underlying error: {e}"
            ) from e

        payload = resp.json()

        if payload.get("IsErroredOnProcessing"):
            msg = payload.get("ErrorMessage") or payload.get("ErrorDetails") or "OCR.space processing error"
            if isinstance(msg, list):
                msg = "; ".join(str(x) for x in msg)
            raise RuntimeError(
                f"OCR.space processing error: {msg}. "
                f"provider={settings.ocr_provider}, "
                f"api_key_present={bool(settings.ocr_space_api_key)}, "
                f"language={settings.ocr_space_language}, "
                f"engine={settings.ocr_space_ocr_engine}"
            )

        lines: list[str] = []
        for item in payload.get("ParsedResults") or []:
            text = (item or {}).get("ParsedText")
            if text:
                lines.append(text)

        return "\n".join(lines).strip()


class PaddleOCRBackend(OCRBackend):
    name = "paddleocr"

    def __init__(self) -> None:
        provider = (settings.ocr_provider or "").strip().lower()
        if provider != "paddleocr":
            raise RuntimeError(
                f"PaddleOCRBackend selected while OCR_PROVIDER is '{settings.ocr_provider}'. "
                "Set OCR_PROVIDER=paddleocr."
            )
        if not settings.enable_paddle_ocr:
            raise RuntimeError("PaddleOCR is disabled. Set ENABLE_PADDLE_OCR=true.")
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except Exception as e:
            raise RuntimeError("PaddleOCR is not installed. Install it and rebuild the image.") from e

        self.ocr = PaddleOCR(
            use_angle_cls=True,
            lang="en",
            show_log=False,
        )

    def extract_text_from_pdf_page(self, pdf_path: Path, page_index: int, scale: float = 3.5) -> str:
        from PIL import Image

        image_bytes = self.render_pdf_page_to_png_bytes(pdf_path, page_index, scale=scale)
        if not image_bytes:
            return ""

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        result = self.ocr.ocr(image)

        lines: list[str] = []
        for block in result or []:
            for line in block or []:
                if len(line) > 1 and line[1]:
                    text = str(line[1][0]).strip()
                    if text:
                        lines.append(text)

        return "\n".join(lines).strip()