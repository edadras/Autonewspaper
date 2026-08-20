"""Font discovery for the internal renderer.

InDesign resolves fonts itself; the built-in preview renderer has to find a
real TrueType file on disk. This module maps a template's font family to the
best installed file, honouring the template's fallback chain and preferring
faces that actually contain the script being typeset.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

log = logging.getLogger(__name__)

FONT_DIRS: list[Path] = []
if sys.platform == "win32":
    FONT_DIRS = [
        Path("C:/Windows/Fonts"),
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    ]
elif sys.platform == "darwin":
    FONT_DIRS = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
else:
    FONT_DIRS = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
    ]

#: Faces known to carry Arabic-script glyphs, tried when a Persian or Arabic
#: family is not installed.
ARABIC_CAPABLE = [
    "Vazirmatn",
    "IRANSans",
    "Sahel",
    "Shabnam",
    "Noto Naskh Arabic",
    "Noto Sans Arabic",
    "Amiri",
    "Scheherazade",
    "Tahoma",
    "Arial",
    "Segoe UI",
    "FreeSerif",
    "DejaVu Sans",
]

LATIN_FALLBACKS = [
    "Source Serif Pro",
    "Georgia",
    "Times New Roman",
    "Liberation Serif",
    "DejaVu Serif",
    "Inter",
    "Segoe UI",
    "Arial",
    "Liberation Sans",
    "DejaVu Sans",
    "FreeSans",
]

STYLE_TOKENS = {
    "black": ["black", "heavy", "extrabold"],
    "bold": ["bold", "semibold", "demibold"],
    "demibold": ["semibold", "demibold", "bold"],
    "semibold": ["semibold", "demibold", "bold"],
    "medium": ["medium", "regular"],
    "light": ["light", "regular"],
    "italic": ["italic", "oblique"],
    "regular": ["regular", "book", "roman"],
}


@lru_cache(maxsize=1)
def _index() -> dict[str, list[Path]]:
    """Index every font file on the machine by lower-cased stem."""
    found: dict[str, list[Path]] = {}
    for directory in FONT_DIRS:
        if not directory.exists():
            continue
        for pattern in ("*.ttf", "*.otf", "*.ttc", "*.TTF", "*.OTF"):
            for path in directory.rglob(pattern):
                found.setdefault(path.stem.lower(), []).append(path)
    log.debug("Indexed %d font file name(s)", len(found))
    return found


def _candidates_for(family: str, style: str) -> list[Path]:
    """Font files matching *family*, ranked by how well they match *style*.

    Files are scored rather than merely filtered: a plain family file wins for
    "Regular", requested style tokens are rewarded, and italic or heavier
    weights are penalised when they were not asked for - otherwise asking for
    Regular can return the italic cut simply because it sorted first.
    """
    index = _index()
    key = family.lower().replace(" ", "")
    style_lower = style.lower()
    wanted = STYLE_TOKENS.get(style_lower, [style_lower, "regular"])
    wants_italic = style_lower in ("italic", "oblique")
    wants_bold = any(token in style_lower for token in ("bold", "black", "heavy"))

    scored: list[tuple[float, Path]] = []
    for stem, paths in index.items():
        compact = stem.lower().replace(" ", "").replace("-", "").replace("_", "")
        if not compact.startswith(key):
            continue
        suffix = compact[len(key) :]
        for path in paths:
            score = 0.0
            if not suffix:
                score -= 3.0 if style_lower in ("regular", "book", "roman") else 0.0
            for position, token in enumerate(wanted):
                if token in suffix:
                    score -= 4.0 - position
                    break
            if not wants_italic and ("italic" in suffix or "oblique" in suffix):
                score += 8.0
            if not wants_bold and ("bold" in suffix or "black" in suffix or "heavy" in suffix):
                score += 4.0
            score += len(suffix) * 0.05
            scored.append((score, path))
    scored.sort(key=lambda item: item[0])
    return [path for _score, path in scored]


def _supports(path: Path, sample: str, size: int = 20) -> bool:
    """Whether a font file renders *sample* with real glyphs."""
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError:
        return False
    try:
        box = font.getbbox(sample)
    except Exception:  # noqa: BLE001
        return False
    if box is None or (box[2] - box[0]) <= 0:
        return False
    try:
        notdef = font.getbbox("\ufffd" * len(sample))
        if notdef and abs((box[2] - box[0]) - (notdef[2] - notdef[0])) < 1:
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


@lru_cache(maxsize=256)
def resolve_font_file(
    family: str, style: str = "Regular", script: str = "latin", fallbacks: tuple[str, ...] = ()
) -> Path | None:
    """Find the best font file for a family/style, or ``None``."""
    sample = "سلام" if script == "arabic" else "Hamburgefonstiv"
    chain = [family, *fallbacks]
    chain += ARABIC_CAPABLE if script == "arabic" else LATIN_FALLBACKS
    seen: set[str] = set()
    for candidate in chain:
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        for path in _candidates_for(candidate, style):
            if _supports(path, sample):
                return path
    # Nothing named matched; take any indexed file that renders the script.
    for paths in _index().values():
        for path in paths:
            if _supports(path, sample):
                return path
    return None


@lru_cache(maxsize=512)
def load_font(
    family: str,
    size_px: int,
    style: str = "Regular",
    script: str = "latin",
    fallbacks: tuple[str, ...] = (),
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a PIL font, degrading to the bitmap default when nothing fits."""
    path = resolve_font_file(family, style, script, fallbacks)
    if path is not None:
        try:
            return ImageFont.truetype(str(path), max(4, int(size_px)))
        except OSError as exc:  # pragma: no cover
            log.debug("Cannot load %s: %s", path, exc)
    return ImageFont.load_default()


def available_families(limit: int = 400) -> list[str]:
    """Font names found on this machine (for the Diagnostics page)."""
    return sorted({stem for stem in _index()})[:limit]


def check_fonts(required: dict[str, str], script: str = "arabic") -> dict[str, str | None]:
    """Report which of the template's fonts are actually installed."""
    out: dict[str, str | None] = {}
    for role, family in required.items():
        path = resolve_font_file(family, "Regular", script)
        out[f"{role}:{family}"] = str(path) if path else None
    return out
