"""Filesystem helpers: safe names, checksums, atomic writes, archives."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def slugify(text: str, max_length: int = 48, default: str = "project") -> str:
    """ASCII slug suitable for a directory name (transliterates when possible)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_text).strip("_").lower()
    if not slug:
        digest = hashlib.sha1((text or default).encode("utf-8")).hexdigest()[:10]
        slug = f"{default}_{digest}"
    return slug[:max_length]


def safe_filename(name: str, fallback: str = "file") -> str:
    """Sanitise *name* so it is a legal file name on Windows and POSIX."""
    cleaned = _UNSAFE.sub("_", (name or "").strip()).rstrip(". ")
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:200]


def unique_path(path: Path) -> Path:
    """Return *path*, or ``name_2.ext``/``name_3.ext`` if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(2, 10_000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}_{datetime.now():%Y%m%d%H%M%S}{suffix}"


def checksum(path: Path | str, algorithm: str = "sha256", chunk: int = 1 << 20) -> str:
    """Hex digest of a file's contents."""
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def write_text_atomic(path: Path | str, content: str, encoding: str = "utf-8") -> Path:
    """Write *content* through a temporary file and replace the target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(target)
    return target


def write_json(path: Path | str, data: Any, indent: int = 2) -> Path:
    """Serialise *data* to JSON atomically."""
    return write_text_atomic(path, json.dumps(data, indent=indent, ensure_ascii=False, default=str))


def read_json(path: Path | str, default: Any = None) -> Any:
    """Read JSON, returning *default* when the file is missing or invalid."""
    file = Path(path)
    if not file.exists():
        return default
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.warning("Cannot read JSON %s: %s", file, exc)
        return default


def copy_into(source: Path | str, target_dir: Path | str, *, rename: str | None = None) -> Path:
    """Copy *source* into *target_dir*, avoiding name collisions."""
    source = Path(source)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_path(target_dir / safe_filename(rename or source.name))
    shutil.copy2(source, destination)
    return destination


def ensure_dir(path: Path | str) -> Path:
    """Create a directory (and parents) and return it."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def human_size(num_bytes: float) -> str:
    """Format a byte count for the UI."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def directory_size(path: Path | str) -> int:
    """Total size in bytes of everything under *path*."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:  # pragma: no cover
                continue
    return total


def make_archive(source_dir: Path | str, target_zip: Path | str, *, exclude: set[str] | None = None) -> Path:
    """Zip *source_dir* into *target_zip* (used by Project Archive export)."""
    source_dir = Path(source_dir)
    target = Path(target_zip)
    target.parent.mkdir(parents=True, exist_ok=True)
    exclude = exclude or set()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in source_dir.rglob("*"):
            if item.is_dir():
                continue
            relative = item.relative_to(source_dir)
            if any(part in exclude for part in relative.parts):
                continue
            archive.write(item, relative.as_posix())
    return target


def extract_archive(archive_path: Path | str, target_dir: Path | str) -> Path:
    """Extract a project archive, refusing entries that escape *target_dir*."""
    target = ensure_dir(target_dir)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            destination = (target / member).resolve()
            if not str(destination).startswith(str(target.resolve())):
                raise ValueError(f"Unsafe archive entry: {member}")
        archive.extractall(target)
    return target


def is_writable(path: Path | str) -> bool:
    """Whether a directory can be written to (used by diagnostics)."""
    directory = Path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def free_space_bytes(path: Path | str) -> int:
    """Free disk space for the volume holding *path*."""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:  # pragma: no cover
        return 0
