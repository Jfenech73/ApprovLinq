from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAME = "ApprovLinq_3_69_phase12_approval_evidence_fact_integrity.zip"
FIXED_TIMESTAMP = (2026, 7, 15, 0, 0, 0)

EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "htmlcov",
    "tmp",
    "venv",
    "__pycache__",
}

EXCLUDED_TOP_LEVEL_DIRS = {
    "data",
}

EXCLUDED_SUFFIXES = {
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".zip",
}

EXCLUDED_FILENAMES = {
    ".env",
    ".coverage",
    "invoice_scanner.db",
}


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = set(rel.parts)
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if rel.parts and rel.parts[0] in EXCLUDED_TOP_LEVEL_DIRS:
        return True
    name = path.name.lower()
    if name in EXCLUDED_FILENAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if name.startswith(("id_rsa", "id_dsa")):
        return True
    if name.endswith(".json") and any(marker in name for marker in ("credential", "credentials", "token", "service-account")):
        return True
    if (name.startswith("phase11_pytest_report") or name.startswith("phase12_pytest")) and path.suffix.lower() == ".txt":
        return True
    return False


def build_zip(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not _is_excluded(path)
    )
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            info = ZipInfo(f"ApprovLinq/{rel}", FIXED_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic ApprovLinq release ZIP.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / DEFAULT_NAME,
        help="Output ZIP path.",
    )
    args = parser.parse_args()
    output = build_zip(args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
