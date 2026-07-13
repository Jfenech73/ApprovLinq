from __future__ import annotations

import re
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]

PROHIBITED_SUFFIXES = {
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
PROHIBITED_PARTS = {
    "data/uploads",
    "data/exports",
    "data/tmp",
    "tmp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
REQUIRED_ENV_KEYS = {
    "APP_NAME",
    "APP_ENV",
    "PORT",
    "DATABASE_URL",
    "UPLOAD_DIR",
    "EXPORT_DIR",
    "FILE_RETENTION_DAYS",
    "OCR_PROVIDER",
    "ENABLE_PADDLE_OCR",
    "OCR_SPACE_API_KEY",
    "OCR_SPACE_ENDPOINT",
    "OCR_SPACE_LANGUAGE",
    "OCR_SPACE_OCR_ENGINE",
    "OCR_SPACE_OVERLAY_REQUIRED",
    "OCR_SPACE_SCALE",
    "OCR_SPACE_TIMEOUT_SECONDS",
    "USE_OPENAI",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "USE_AZURE_DI",
    "AZURE_DI_ENDPOINT",
    "AZURE_DI_KEY",
    "AZURE_DI_PAGE_TIMEOUT_S",
    "AZURE_DI_ORIENTATION_TIMEOUT_S",
    "AZURE_DI_ORIENTATION_ENABLED",
    "LOCAL_ORIENTATION_ENABLED",
    "LOCAL_ORIENTATION_SAMPLE_PAGES",
    "AZURE_DI_READ_TEXT_FALLBACK",
    "EXTRACTION_PAGE_TIMEOUT_S",
    "EXTRACTION_CONSECUTIVE_TIMEOUT_LIMIT",
    "NORMALIZE_PAGE_ORIENTATION",
    "SCAN_PROVIDER_BASELINE_MODE",
    "APPROVLINQ_START_SCAN_WORKER",
}
SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://[^:\s/]+:[^@\s]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile("Account" + r"Key=[^;\s]+", re.IGNORECASE),
]
TEXT_SUFFIXES = {
    "",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".txt",
    ".yaml",
    ".yml",
}


def _normalise(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _is_prohibited_path(path: str, *, include_generated_dirs: bool = True) -> bool:
    normalised = _normalise(path)
    lower = normalised.lower()
    name = lower.rsplit("/", 1)[-1]

    if name == ".env":
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if Path(name).suffix in PROHIBITED_SUFFIXES:
        return True
    if name.startswith(("id_rsa", "id_dsa")):
        return True
    if name.endswith(".json") and any(marker in name for marker in ("credential", "credentials", "token", "service-account")):
        return True
    return include_generated_dirs and any(part in lower for part in PROHIBITED_PARTS)


def test_no_prohibited_secret_or_runtime_files_in_source_tree():
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(".git/"):
            continue
        if _is_prohibited_path(rel, include_generated_dirs=False):
            offenders.append(rel)

    assert offenders == []


def test_distributable_zips_do_not_contain_secret_or_runtime_files():
    zip_paths = sorted(ROOT.glob("*.zip")) + sorted((ROOT / "dist").glob("*.zip"))
    assert zip_paths, "Expected at least one distributable zip to scan"

    offenders: list[str] = []
    for zip_path in zip_paths:
        with ZipFile(zip_path) as archive:
            for name in archive.namelist():
                if _is_prohibited_path(name, include_generated_dirs=True):
                    offenders.append(f"{zip_path.name}:{name}")

    assert offenders == []


def test_source_and_distributable_text_do_not_contain_credential_shaped_values():
    offenders: list[str] = []

    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS):
            offenders.append(rel)

    for zip_path in sorted(ROOT.glob("*.zip")) + sorted((ROOT / "dist").glob("*.zip")):
        with ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if Path(info.filename).suffix.lower() not in TEXT_SUFFIXES:
                    continue
                text = archive.read(info).decode("utf-8", errors="ignore")
                if any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS):
                    offenders.append(f"{zip_path.name}:{info.filename}")

    assert offenders == []


def test_env_example_contains_names_only():
    env_example = ROOT / ".env.example"
    assert env_example.exists()

    keys: set[str] = set()
    offenders: list[str] = []
    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"Expected KEY= format: {raw_line!r}"
        key, value = line.split("=", 1)
        keys.add(key)
        if value.strip():
            offenders.append(key)

    assert REQUIRED_ENV_KEYS <= keys
    assert offenders == []


def test_ignore_files_and_docker_context_exclude_secrets():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for pattern in [".env", ".env.*", "data/uploads/", "data/exports/", "*.db", "*.pem", "*credentials*.json"]:
        assert pattern in gitignore
        assert pattern in dockerignore

    assert "COPY ApprovLinq/requirements.txt /app/requirements.txt" in dockerfile
    assert "COPY ApprovLinq /app" in dockerfile
    assert "APPROVLINQ_START_SCAN_WORKER" in dockerfile
    assert "python scripts/scan_worker.py" in dockerfile
    assert "COPY requirements.txt /app/requirements.txt" not in dockerfile
    assert "COPY . /app" not in dockerfile
