"""Looking a format up, or building one from whatever the operator typed.

The catalogue covers the sizes people ask for by name. Everything else -
"70x100 cm", "1080 x 1350 px", "5.5 in x 8.5 in" - is parsed into a format of
its own, so an unusual size is an ordinary input rather than an error.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.errors import AppError, Component
from app.formats.catalog import _SIZE_RE, _UNIT_ALIASES, Format, Medium, SafeArea, Unit
from app.formats.presets import ALL_FORMATS

log = logging.getLogger(__name__)

#: Words that pick an orientation when a named format is asked for.
_LANDSCAPE_WORDS = {"landscape", "horizontal", "wide", "افقی", "yatay", "أفقي"}
_PORTRAIT_WORDS = {"portrait", "vertical", "tall", "عمودی", "dikey", "عمودي"}

_PUNCT_RE = re.compile(r"[\s_\-/]+")


class UnknownFormatError(AppError):
    """The operator asked for a size that is neither named nor parsable."""

    component = Component.CORE
    recovery_action = "Give a size such as '70x100 cm', '1080x1350 px' or a name such as 'A3'."


def _key(text: str) -> str:
    """Normalise a name for lookup."""
    return _PUNCT_RE.sub("_", text.strip().lower()).strip("_")


class FormatRegistry:
    """The named formats, plus anything the operator describes by size."""

    def __init__(self, formats: list[Format] | None = None) -> None:
        self._formats: dict[str, Format] = {}
        for item in formats if formats is not None else ALL_FORMATS:
            self.add(item)

    # ---------------------------------------------------------------- data
    def add(self, item: Format) -> Format:
        """Register a format under its id and its aliases."""
        self._formats[_key(item.id)] = item
        for alias in item.aliases:
            self._formats.setdefault(_key(alias), item)
        self._formats.setdefault(_key(item.name), item)
        return item

    def all(self) -> list[Format]:
        """Every distinct format, in catalogue order."""
        seen: dict[int, Format] = {}
        for item in self._formats.values():
            seen.setdefault(id(item), item)
        return list(seen.values())

    def by_medium(self, medium: Medium | str) -> list[Format]:
        """Every format for one medium."""
        wanted = Medium(medium) if isinstance(medium, str) else medium
        return [item for item in self.all() if item.medium is wanted]

    def get(self, name: str) -> Format | None:
        """Look a format up by id, alias or name."""
        return self._formats.get(_key(name))

    # ------------------------------------------------------------ resolving
    def resolve(self, request: str, *, dpi: int | None = None) -> Format:
        """Turn what the operator typed into a format.

        Accepts a catalogue name ("A3", "Instagram story"), a name with an
        orientation ("A3 landscape"), or a bare size in any supported unit
        ("70x100cm", "1080 x 1350 px", '8.5" x 11"').
        """
        text = (request or "").strip()
        if not text:
            raise UnknownFormatError("No format was given")

        found = self.get(text)
        if found is not None:
            return found.at_dpi(dpi) if dpi else found

        words = _PUNCT_RE.sub(" ", text.lower()).split()
        wanted_orientation = None
        if any(word in _LANDSCAPE_WORDS for word in words):
            wanted_orientation = "landscape"
        elif any(word in _PORTRAIT_WORDS for word in words):
            wanted_orientation = "portrait"
        if wanted_orientation:
            stripped = " ".join(
                word for word in words if word not in _LANDSCAPE_WORDS and word not in _PORTRAIT_WORDS
            )
            base = self.get(stripped)
            if base is not None:
                oriented = base if base.orientation == wanted_orientation else base.rotated()
                return oriented.at_dpi(dpi) if dpi else oriented

        parsed = self.parse_size(text, dpi=dpi)
        if parsed is not None:
            return parsed

        # "billboard 6x3 m" - a catalogue name with its size spelled out, or a
        # name surrounded by words. The longest matching name wins so
        # "instagram story" beats "instagram".
        named = self._longest_named(text)
        if named is not None:
            return named.at_dpi(dpi) if dpi else named
        raise UnknownFormatError(
            f"'{request}' is not a format this studio knows, and is not a size it can read",
            context={"examples": ["A3", "Instagram story", "70x100 cm", "1080x1350 px"]},
        )

    def _longest_named(self, text: str) -> Format | None:
        """The longest catalogue name that appears inside *text*."""
        haystack = f" {_key(text)} "
        best: Format | None = None
        best_length = 0
        for name, item in self._formats.items():
            if len(name) > best_length and f"_{name}_" in haystack.replace(" ", "_"):
                best, best_length = item, len(name)
        return best

    def parse_size(self, text: str, *, dpi: int | None = None) -> Format | None:
        """Build a format from a bare ``WIDTHxHEIGHT UNIT`` string."""
        match = _SIZE_RE.match(text)
        if not match:
            return None
        first = match.group("wunit").strip().lower()
        second = match.group("hunit").strip().lower()
        if first and second and first != second:
            raise UnknownFormatError(
                f"'{text}' mixes units: {first} and {second}",
                context={"width_unit": first, "height_unit": second},
            )
        unit = _UNIT_ALIASES.get(second or first)
        if unit is None:
            return None
        width = float(match.group("w").replace(",", "."))
        height = float(match.group("h").replace(",", "."))
        if width <= 0 or height <= 0:
            raise UnknownFormatError(f"'{text}' has a side of zero or less")

        pixels = unit is Unit.PX
        resolution = dpi or (72 if pixels else 300)
        return Format(
            id=f"custom_{width:g}x{height:g}{unit.value}",
            name=f"{width:g} × {height:g} {unit.value}",
            medium=Medium.SCREEN if pixels else Medium.PRINT,
            width=width,
            height=height,
            unit=unit,
            dpi=resolution,
            bleed_mm=0.0 if pixels else 3.0,
            notes="Given by size rather than chosen from the catalogue.",
        )

    def custom(
        self,
        width: float,
        height: float,
        unit: Unit | str = Unit.MM,
        *,
        name: str = "",
        medium: Medium | str | None = None,
        dpi: int | None = None,
        bleed_mm: float | None = None,
        fps: float = 0.0,
        duration_seconds: float = 0.0,
        safe_area: SafeArea | None = None,
    ) -> Format:
        """Build a format from explicit numbers."""
        unit = Unit(unit) if isinstance(unit, str) else unit
        if width <= 0 or height <= 0:
            raise UnknownFormatError(
                "A format needs a positive width and height",
                context={"width": width, "height": height},
            )
        pixels = unit is Unit.PX
        if medium is None:
            resolved_medium = Medium.VIDEO if fps > 0 else (Medium.SCREEN if pixels else Medium.PRINT)
        else:
            resolved_medium = Medium(medium) if isinstance(medium, str) else medium
        return Format(
            id=f"custom_{width:g}x{height:g}{unit.value}",
            name=name or f"{width:g} × {height:g} {unit.value}",
            medium=resolved_medium,
            width=width,
            height=height,
            unit=unit,
            dpi=dpi or (72 if pixels else 300),
            bleed_mm=(0.0 if pixels else 3.0) if bleed_mm is None else bleed_mm,
            fps=fps,
            duration_seconds=duration_seconds,
            safe_area=safe_area or SafeArea(),
        )

    def search(self, term: str, limit: int = 12) -> list[Format]:
        """Formats whose name, id or alias contains *term*."""
        needle = _key(term)
        if not needle:
            return self.all()[:limit]
        hits: list[Format] = []
        for item in self.all():
            haystack = " ".join([_key(item.id), _key(item.name), *(_key(a) for a in item.aliases)])
            if needle in haystack:
                hits.append(item)
        return hits[:limit]

    def describe(self) -> dict[str, Any]:
        """Summary for the diagnostics page."""
        counts: dict[str, int] = {}
        for item in self.all():
            counts[item.medium.value] = counts.get(item.medium.value, 0) + 1
        return {"formats": len(self.all()), "by_medium": counts}


#: The catalogue every part of the application shares.
FORMATS = FormatRegistry()
