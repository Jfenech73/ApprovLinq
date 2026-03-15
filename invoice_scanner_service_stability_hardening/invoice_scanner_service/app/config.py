from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "Development"
    app_name: str = "Invoice Scanner Service"
    database_url: str = "sqlite:///./invoice_scanner.db"

    upload_dir: str = "./data/uploads"
    export_dir: str = "./data/exports"

    # OCR
    ocr_provider: str = "none"   # none | ocr_space | paddleocr
    enable_paddle_ocr: bool = False

    # OCR.space
    ocr_space_api_key: str | None = None
    ocr_space_endpoint: str = "https://api.ocr.space/parse/image"
    ocr_space_language: str = "auto"
    ocr_space_ocr_engine: int = 2
    ocr_space_overlay_required: bool = False
    ocr_space_scale: bool = True
    ocr_space_timeout_seconds: int = 90

    # OpenAI
    use_openai: bool = False
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    # Stability / timeouts / logging
    page_processing_timeout_seconds: int = 120
    openai_timeout_seconds: int = 45
    openai_summary_timeout_seconds: int = 30
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def upload_path(self) -> Path:
        path = Path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def export_path(self) -> Path:
        path = Path(self.export_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def diagnostics(self) -> dict:
        return {
            "app_env": self.app_env,
            "ocr_provider": self.ocr_provider,
            "enable_paddle_ocr": self.enable_paddle_ocr,
            "ocr_space_api_key_present": bool(self.ocr_space_api_key),
            "ocr_space_endpoint": self.ocr_space_endpoint,
            "ocr_space_language": self.ocr_space_language,
            "ocr_space_ocr_engine": self.ocr_space_ocr_engine,
            "ocr_space_timeout_seconds": self.ocr_space_timeout_seconds,
            "use_openai": self.use_openai,
            "openai_api_key_present": bool(self.openai_api_key),
            "openai_model": self.openai_model,
            "page_processing_timeout_seconds": self.page_processing_timeout_seconds,
            "openai_timeout_seconds": self.openai_timeout_seconds,
            "openai_summary_timeout_seconds": self.openai_summary_timeout_seconds,
            "log_level": self.log_level,
            "upload_dir": self.upload_dir,
            "export_dir": self.export_dir,
        }


settings = Settings()