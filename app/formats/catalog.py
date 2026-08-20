"""Every size the studio can produce.

The application started as a newspaper tool, where the page was whatever the
template said. A poster, a book cover, an Instagram story and a 4K sequence
are all just other formats, so they live in one catalogue rather than being
special cases: a format knows its size, the unit that size is naturally
expressed in, the resolution it is produced at, and - where the platform
imposes one - the safe area inside which nothing important may sit.

Anything the catalogue does not name can still be asked for by size, in
millimetres, centimetres, inches, points, picas or pixels.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0
PICA_PER_INCH = 6.0


class Medium(str, Enum):
    """What a format is ultimately produced for."""

    PRINT = "print"
    """Ink on paper: sized in millimetres, needs bleed and a print resolution."""
    SCREEN = "screen"
    """A still image for a screen: sized in pixels."""
    SOCIAL = "social"
    """A screen format a platform defines, usually with a safe area."""
    VIDEO = "video"
    """A moving sequence: pixels, a frame rate and a duration."""


class Unit(str, Enum):
    """Units a size can be given in."""

    MM = "mm"
    CM = "cm"
    M = "m"
    IN = "in"
    PT = "pt"
    PICA = "pica"
    PX = "px"


#: Conversion to millimetres for the physical units.
_TO_MM = {
    Unit.MM: 1.0,
    Unit.CM: 10.0,
    Unit.M: 1000.0,
    Unit.IN: MM_PER_INCH,
    Unit.PT: MM_PER_INCH / PT_PER_INCH,
    Unit.PICA: MM_PER_INCH / PICA_PER_INCH,
}

#: ``WIDTH[unit] x HEIGHT[unit]``. The unit may follow either number - people
#: write both `8.5in x 11in` and `70x100 cm` - and the two must agree.
_SIZE_RE = re.compile(
    r"^\s*(?P<w>\d+(?:[.,]\d+)?)\s*(?P<wunit>[a-zA-Z\"\u2032\u2033']*)"
    r"\s*(?:x|×|\*|by|در)\s*"
    r"(?P<h>\d+(?:[.,]\d+)?)\s*(?P<hunit>[a-zA-Z\"\u2032\u2033']*)\s*$",
    re.IGNORECASE,
)

_UNIT_ALIASES = {
    "": Unit.MM,
    "mm": Unit.MM,
    "millimetre": Unit.MM,
    "millimeter": Unit.MM,
    "cm": Unit.CM,
    "centimetre": Unit.CM,
    "centimeter": Unit.CM,
    "m": Unit.M,
    "metre": Unit.M,
    "meter": Unit.M,
    "metres": Unit.M,
    "meters": Unit.M,
    "in": Unit.IN,
    "inch": Unit.IN,
    "inches": Unit.IN,
    '"': Unit.IN,
    "pt": Unit.PT,
    "point": Unit.PT,
    "points": Unit.PT,
    "pc": Unit.PICA,
    "pica": Unit.PICA,
    "picas": Unit.PICA,
    "px": Unit.PX,
    "pixel": Unit.PX,
    "pixels": Unit.PX,
}


@dataclass(frozen=True)
class SafeArea:
    """Margins a platform reserves for its own interface, as a fraction.

    Instagram puts the caption and buttons over the bottom of a story; a
    broadcast frame keeps titles away from the edge. Anything that must be
    read goes inside this.
    """

    top: float = 0.0
    bottom: float = 0.0
    left: float = 0.0
    right: float = 0.0

    def is_empty(self) -> bool:
        """Whether the platform reserves nothing."""
        return not (self.top or self.bottom or self.left or self.right)

    def inset_px(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The safe rectangle in pixels: ``(x, y, width, height)``."""
        left = int(round(width * self.left))
        top = int(round(height * self.top))
        return (
            left,
            top,
            max(1, width - left - int(round(width * self.right))),
            max(1, height - top - int(round(height * self.bottom))),
        )


@dataclass(frozen=True)
class Format:
    """One producible size.

    A format is stored in whichever unit it is naturally specified in - a
    poster in millimetres, an Instagram story in pixels - and converts to the
    other on request, so the layout engine can work in millimetres whatever
    the destination is.
    """

    id: str
    name: str
    medium: Medium
    width: float
    height: float
    unit: Unit
    dpi: int = 300
    bleed_mm: float = 0.0
    safe_area: SafeArea = field(default_factory=SafeArea)
    fps: float = 0.0
    """Frames per second, for a video format."""
    duration_seconds: float = 0.0
    """Default duration, for a video format."""
    aliases: tuple[str, ...] = ()
    notes: str = ""

    # ------------------------------------------------------------ geometry
    @property
    def width_mm(self) -> float:
        """Width in millimetres."""
        return self._to_mm(self.width)

    @property
    def height_mm(self) -> float:
        """Height in millimetres."""
        return self._to_mm(self.height)

    @property
    def width_px(self) -> int:
        """Width in pixels at this format's resolution."""
        return self._to_px(self.width)

    @property
    def height_px(self) -> int:
        """Height in pixels at this format's resolution."""
        return self._to_px(self.height)

    @property
    def aspect(self) -> float:
        """Width divided by height."""
        return self.width / max(1e-9, self.height)

    @property
    def orientation(self) -> str:
        """``portrait``, ``landscape`` or ``square``."""
        if abs(self.aspect - 1.0) < 0.02:
            return "square"
        return "landscape" if self.aspect > 1.0 else "portrait"

    @property
    def is_physical(self) -> bool:
        """Whether the size is a physical measurement rather than pixels."""
        return self.unit is not Unit.PX

    def _to_mm(self, value: float) -> float:
        if self.unit is Unit.PX:
            return value / max(1, self.dpi) * MM_PER_INCH
        return value * _TO_MM[self.unit]

    def _to_px(self, value: float) -> int:
        if self.unit is Unit.PX:
            return int(round(value))
        return int(round(self._to_mm(value) / MM_PER_INCH * self.dpi))

    #: Ratios worth naming, because a designer thinks in them.
    COMMON_RATIOS = (
        (1, 1),
        (4, 3),
        (3, 4),
        (3, 2),
        (2, 3),
        (16, 9),
        (9, 16),
        (5, 4),
        (4, 5),
        (16, 10),
        (21, 9),
        (2, 1),
        (1, 2),
    )

    def aspect_label(self) -> str:
        """The aspect as a ratio, named where one is recognisable.

        A pixel format reduces exactly; a paper size does not - A4 in whole
        pixels is 2480:3508, which tells nobody anything - so a physical
        format is matched against the ratios a designer works in and only
        falls back to a decimal when it is genuinely none of them.
        """
        if not self.is_physical:
            width, height = self.width_px, self.height_px
            divisor = math.gcd(width, height) or 1
            return f"{width // divisor}:{height // divisor}"
        aspect = self.aspect
        for across, down in self.COMMON_RATIOS:
            if abs(aspect - across / down) <= 0.01:
                return f"{across}:{down}"
        return f"{aspect:.3f}:1"

    def describe(self) -> str:
        """One line for a menu or a log."""
        if self.is_physical:
            size = f"{self.width_mm:.0f} × {self.height_mm:.0f} mm"
        else:
            size = f"{self.width_px} × {self.height_px} px"
        extra = f", {self.fps:g} fps" if self.medium is Medium.VIDEO else ""
        return f"{self.name} ({size}{extra})"

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "id": self.id,
            "name": self.name,
            "medium": self.medium.value,
            "width": self.width,
            "height": self.height,
            "unit": self.unit.value,
            "width_mm": round(self.width_mm, 3),
            "height_mm": round(self.height_mm, 3),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "bleed_mm": self.bleed_mm,
            "aspect": self.aspect_label(),
            "orientation": self.orientation,
            "fps": self.fps,
            "duration_seconds": self.duration_seconds,
            "safe_area": {
                "top": self.safe_area.top,
                "bottom": self.safe_area.bottom,
                "left": self.safe_area.left,
                "right": self.safe_area.right,
            },
            "notes": self.notes,
        }

    def rotated(self) -> Format:
        """The same format with width and height exchanged."""
        from dataclasses import replace

        return replace(self, id=f"{self.id}_rotated", width=self.height, height=self.width)

    def at_dpi(self, dpi: int) -> Format:
        """The same format produced at another resolution."""
        from dataclasses import replace

        return replace(self, dpi=max(1, dpi))
