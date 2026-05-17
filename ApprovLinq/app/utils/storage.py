from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.config import settings


def _normalize_relative(raw_path: str, marker: str) -> Path:
    text = str(raw_path or '').replace('\\', '/').strip()
    p = Path(text)
    if p.is_absolute():
        parts = list(p.parts)
        if marker in parts:
            idx = parts.index(marker)
            return Path(*parts[idx + 1:])
        return Path(p.name)
    parts = [part for part in p.parts if part not in ('.', '')]
    if marker in parts:
        idx = parts.index(marker)
        return Path(*parts[idx + 1:])
    if len(parts) >= 2 and parts[0] == 'data' and parts[1] == marker:
        return Path(*parts[2:])
    if len(parts) >= 3 and parts[0] == 'app' and parts[1].lower() == 'data' and parts[2] == marker:
        return Path(*parts[3:])
    return Path(*parts)


def _candidate_paths(raw_path: str, root: Path, marker: str) -> Iterable[Path]:
    text = str(raw_path or '').strip()
    if not text:
        return []
    p = Path(text)
    out: list[Path] = []
    if p.is_absolute():
        out.append(p)
    else:
        out.append((Path.cwd() / p).resolve())
    rel = _normalize_relative(text, marker)
    if rel != Path('.'):
        out.append((root / rel).resolve())
    out.append((root / Path(text).name).resolve())
    seen = set()
    uniq = []
    for cand in out:
        key = str(cand)
        if key not in seen:
            uniq.append(cand)
            seen.add(key)
    return uniq


def resolve_upload_path(raw_path: str) -> Path:
    root = settings.upload_path
    candidates = list(_candidate_paths(raw_path, root, 'uploads'))
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0] if candidates else root


def resolve_export_path(raw_path: str) -> Path:
    root = settings.export_path
    candidates = list(_candidate_paths(raw_path, root, 'exports'))
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0] if candidates else root


def batch_upload_folder(batch_id) -> Path:
    folder = settings.upload_path / str(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def batch_export_folder(batch_id) -> Path:
    folder = settings.export_path / str(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
