from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Invoice Scanner Service"
    app_env: str = "development"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    database_url: str = "sqlite:///./invoice_scanner.db"
    upload_dir: str = "./data/uploads"
    export_dir: str = "./data/exports"
    use_openai: bool = True

    # OCR controls
    ocr_provider: str = "ocr_space"  # ocr_space | paddleocr | none
    enable_paddle_ocr: bool = False  # backward compatibility
    tesseract_cmd: str | None = None  # backward compatibility only; no longer used by default
    ocr_space_api_key: str | None = None
    ocr_space_endpoint: str = "https://api.ocr.space/parse/image"
    ocr_space_language: str = "auto"
    ocr_space_overlay_required: bool = False
    ocr_space_scale: bool = True
    ocr_space_ocr_engine: int = 2
    ocr_space_timeout_seconds: int = 90

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def export_path(self) -> Path:
        p = Path(self.export_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

settings = Settings()
