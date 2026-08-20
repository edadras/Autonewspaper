"""Measurement conversions.

Millimetres are the internal unit for every layout coordinate. InDesign is
driven in millimetres, Photoshop and the preview renderer in pixels, and
typography in points.
"""

from __future__ import annotations

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0


def mm_to_pt(mm: float) -> float:
    """Millimetres to points."""
    return mm * PT_PER_INCH / MM_PER_INCH


def pt_to_mm(pt: float) -> float:
    """Points to millimetres."""
    return pt * MM_PER_INCH / PT_PER_INCH


def mm_to_px(mm: float, dpi: float) -> float:
    """Millimetres to pixels at *dpi*."""
    return mm * dpi / MM_PER_INCH


def px_to_mm(px: float, dpi: float) -> float:
    """Pixels at *dpi* to millimetres."""
    return px * MM_PER_INCH / dpi


def mm_to_inch(mm: float) -> float:
    """Millimetres to inches."""
    return mm / MM_PER_INCH


def effective_dpi(pixel_size: int, printed_mm: float) -> float:
    """Resolution an image is actually reproduced at when scaled to *printed_mm*."""
    if printed_mm <= 0:
        return 0.0
    return pixel_size / (printed_mm / MM_PER_INCH)


PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "Broadsheet": (297.0, 420.0),
    "Broadsheet Large": (315.0, 470.0),
    "Berliner": (315.0, 470.0),
    "Tabloid": (280.0, 400.0),
    "Compact": (210.0, 297.0),
    "A2": (420.0, 594.0),
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "Letter": (215.9, 279.4),
    "Magazine": (210.0, 280.0),
    "Poster A1": (594.0, 841.0),
    "Square 250": (250.0, 250.0),
}


def page_size(name: str, default: tuple[float, float] = (297.0, 420.0)) -> tuple[float, float]:
    """Look up a named page size in millimetres."""
    return PAGE_SIZES_MM.get(name, default)


def aspect_ratio_size(ratio: str, long_edge_px: int = 1536) -> tuple[int, int]:
    """Pixel dimensions for an ``"16:9"`` style aspect ratio."""
    try:
        left, right = ratio.split(":")
        width_ratio, height_ratio = float(left), float(right)
    except (ValueError, ZeroDivisionError):
        width_ratio, height_ratio = 16.0, 9.0
    if width_ratio <= 0 or height_ratio <= 0:
        width_ratio, height_ratio = 16.0, 9.0
    if width_ratio >= height_ratio:
        width = long_edge_px
        height = int(round(long_edge_px * height_ratio / width_ratio))
    else:
        height = long_edge_px
        width = int(round(long_edge_px * width_ratio / height_ratio))
    return (max(64, width - width % 8), max(64, height - height % 8))


def closest_aspect_ratio(width: float, height: float) -> str:
    """Nearest common aspect ratio label for a frame."""
    if height <= 0:
        return "1:1"
    candidates = {
        "1:1": 1.0,
        "4:3": 4 / 3,
        "3:2": 1.5,
        "16:9": 16 / 9,
        "21:9": 21 / 9,
        "3:4": 0.75,
        "2:3": 2 / 3,
        "9:16": 9 / 16,
    }
    target = width / height
    return min(candidates.items(), key=lambda kv: abs(kv[1] - target))[0]
