"""A style brief: what a reference means for the thing being made.

The measurements say what a reference *is*. This says what to *do* with it -
which colours become the palette, what kind of type belongs, how much air the
composition wants, what a matching edit would feel like.

Two things produce it. The measured facts are turned into decisions by rules
that any designer would recognise, and those decisions hold whatever any
model says. The vision model is then asked for the part that is genuinely
judgement - what the reference evokes, what it is for, what it is avoiding -
and is shown the measurements so it works from them. Where the two disagree
about something measurable, the measurement wins and the disagreement is
recorded rather than hidden.
"""

from __future__ import annotations

import colorsys
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from app.creative.reference import Measurements, Swatch

log = logging.getLogger(__name__)

Temperature = Literal["warm", "cool", "neutral"]
Density = Literal["airy", "balanced", "dense"]
Weight = Literal["light", "regular", "medium", "bold", "black"]


@dataclass
class TypeStyle:
    """What kind of type belongs with a reference."""

    display_family: str = ""
    """A named face, when one was asked for; otherwise empty and the
    characteristics below choose one."""
    body_family: str = ""
    serif: bool = False
    display_weight: Weight = "bold"
    body_weight: Weight = "regular"
    scale_ratio: float = 1.5
    """Step between one size and the next - 1.2 is quiet, 1.6 is dramatic."""
    tracking: float = 0.0
    all_caps: bool = False
    justified: bool = False

    def sizes(self, base_pt: float, steps: int = 5) -> list[float]:
        """A type scale from *base_pt* upwards, in this style's ratio."""
        return [round(base_pt * self.scale_ratio**step, 2) for step in range(steps)]

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "display_family": self.display_family,
            "body_family": self.body_family,
            "serif": self.serif,
            "display_weight": self.display_weight,
            "body_weight": self.body_weight,
            "scale_ratio": self.scale_ratio,
            "tracking": self.tracking,
            "all_caps": self.all_caps,
            "justified": self.justified,
        }


@dataclass
class StyleBrief:
    """Everything a design should inherit from its references."""

    palette: list[Swatch] = field(default_factory=list)
    background: str = "#ffffff"
    foreground: str = "#111111"
    accent: str = "#c2410c"
    temperature: Temperature = "neutral"
    density: Density = "balanced"
    contrast: float = 50.0
    """0-100: how far apart the light and dark ends should sit."""
    typography: TypeStyle = field(default_factory=TypeStyle)
    mood: list[str] = field(default_factory=list)
    """What it evokes, in the operator's words - judgement, from the model."""
    avoid: list[str] = field(default_factory=list)
    era: str = ""
    motion: str = ""
    """For a moving reference: how a matching edit should move."""
    sources: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    """Where the model said something the pixels contradict."""
    notes: str = ""

    def swatch(self, role: str) -> str | None:
        """The colour playing a given role, if there is one."""
        return next((s.hex for s in self.palette if s.role == role), None)

    def contrast_ratio(self) -> float:
        """WCAG contrast between the foreground and the background."""
        return contrast_ratio(self.foreground, self.background)

    def readable(self) -> bool:
        """Whether body text in these colours can actually be read."""
        return self.contrast_ratio() >= 4.5

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "palette": [s.to_dict() for s in self.palette],
            "background": self.background,
            "foreground": self.foreground,
            "accent": self.accent,
            "temperature": self.temperature,
            "density": self.density,
            "contrast": round(self.contrast, 1),
            "contrast_ratio": round(self.contrast_ratio(), 2),
            "typography": self.typography.to_dict(),
            "mood": self.mood,
            "avoid": self.avoid,
            "era": self.era,
            "motion": self.motion,
            "sources": self.sources,
            "disagreements": self.disagreements,
            "notes": self.notes,
        }

    def describe(self) -> str:
        """A sentence a person would recognise."""
        parts = [f"{self.temperature} and {self.density}"]
        if self.mood:
            parts.append("/".join(self.mood[:3]))
        parts.append(f"{self.typography.display_weight} display type")
        parts.append(f"accent {self.accent}")
        return ", ".join(parts)


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a colour."""
    text = (hex_color or "#000000").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    channels = []
    for index in (0, 2, 4):
        try:
            value = int(text[index : index + 2], 16) / 255
        except ValueError:
            value = 0.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(left: str, right: str) -> float:
    """WCAG contrast ratio between two colours, 1 to 21."""
    a, b = relative_luminance(left), relative_luminance(right)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def readable_on(background: str, candidates: tuple[str, ...] = ("#ffffff", "#111111")) -> str:
    """Whichever candidate reads best on *background*."""
    return max(candidates, key=lambda colour: contrast_ratio(colour, background))


def shift(hex_color: str, *, lighten: float = 0.0, saturate: float = 0.0) -> str:
    """Nudge a colour's lightness and saturation, staying in gamut."""
    text = (hex_color or "#000000").lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        r, g, b = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return hex_color
    hue, lightness, saturation_value = colorsys.rgb_to_hls(r, g, b)
    lightness = max(0.0, min(1.0, lightness + lighten))
    saturation_value = max(0.0, min(1.0, saturation_value + saturate))
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation_value)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def brief_from_measurements(measurements: Measurements, *, source: str = "") -> StyleBrief:
    """Turn measured facts into design decisions.

    Every rule here is one a designer would state out loud: a dark reference
    wants a dark ground; a busy one wants quieter type and more air; a
    high-contrast one can carry a heavier display face; an accent has to be
    the least common saturated colour or it stops being an accent.
    """
    brief = StyleBrief(palette=list(measurements.palette))
    if source:
        brief.sources.append(source)

    brief.temperature = (
        "warm" if measurements.warmth > 12 else "cool" if measurements.warmth < -12 else "neutral"
    )
    brief.contrast = measurements.contrast * 2.2
    brief.density = (
        "dense"
        if measurements.busyness > 26 or measurements.empty_share < 0.18
        else "airy"
        if measurements.busyness < 10 or measurements.empty_share > 0.45
        else "balanced"
    )

    dark = measurements.brightness < 42
    neutrals = [s for s in brief.palette if s.is_neutral()]
    coloured = [s for s in brief.palette if not s.is_neutral()]

    # The ground is the largest area that can carry text, which is usually a
    # neutral; failing that, the dominant colour pushed towards an extreme.
    if neutrals:
        ground = (
            min(neutrals, key=lambda s: s.luminance) if dark else max(neutrals, key=lambda s: s.luminance)
        )
        brief.background = ground.hex
    elif coloured:
        brief.background = shift(coloured[0].hex, lighten=-0.28 if dark else 0.34, saturate=-0.2)
    else:
        brief.background = "#111111" if dark else "#ffffff"

    brief.foreground = readable_on(brief.background)
    accent = brief.swatch("accent") or (coloured[-1].hex if coloured else None)
    if accent:
        brief.accent = accent
    # An accent nobody can see is not an accent: push it until it separates.
    tries = 0
    while contrast_ratio(brief.accent, brief.background) < 2.6 and tries < 6:
        brief.accent = shift(brief.accent, lighten=0.1 if dark else -0.1, saturate=0.06)
        tries += 1

    brief.typography = _type_from(measurements, brief)
    brief.notes = measurements.describe()
    return brief


def _type_from(measurements: Measurements, brief: StyleBrief) -> TypeStyle:
    """What kind of type belongs with these measurements."""
    style = TypeStyle()
    # A busy frame needs type that stays out of the way; a quiet one can carry
    # a dramatic scale because there is room for it to breathe.
    if brief.density == "dense":
        style.scale_ratio = 1.25
        style.display_weight = "bold"
    elif brief.density == "airy":
        style.scale_ratio = 1.7
        style.display_weight = "black" if measurements.contrast > 30 else "medium"
    else:
        style.scale_ratio = 1.5
        style.display_weight = "bold"

    # Low contrast and low saturation reads as editorial and restrained,
    # which is where a serif belongs; a saturated high-contrast frame is
    # commercial, and wants a grotesque.
    style.serif = measurements.saturation < 28 and measurements.contrast < 34
    style.body_weight = "regular"
    style.tracking = -10 if style.display_weight in ("bold", "black") else 0
    style.all_caps = measurements.contrast > 40 and brief.density != "dense"
    style.justified = brief.density == "dense"
    return style


def merge_briefs(briefs: list[StyleBrief]) -> StyleBrief:
    """One brief from several references, weighted by how much each says."""
    if not briefs:
        return StyleBrief()
    if len(briefs) == 1:
        return briefs[0]
    merged = StyleBrief()
    merged.sources = [source for brief in briefs for source in brief.sources]
    # Colour is additive: every reference contributes swatches, and the ones
    # that appear in more than one reference matter most.
    seen: dict[str, Swatch] = {}
    for brief in briefs:
        for swatch in brief.palette:
            existing = seen.get(swatch.hex)
            if existing is None:
                seen[swatch.hex] = Swatch(hex=swatch.hex, share=swatch.share, role=swatch.role)
            else:
                existing.share += swatch.share
    merged.palette = sorted(seen.values(), key=lambda s: -s.share)[:8]

    merged.background = _most_common(brief.background for brief in briefs)
    merged.foreground = readable_on(merged.background)
    merged.accent = _most_common(brief.accent for brief in briefs)
    merged.temperature = _most_common(brief.temperature for brief in briefs)  # type: ignore[assignment]
    merged.density = _most_common(brief.density for brief in briefs)  # type: ignore[assignment]
    merged.contrast = sum(brief.contrast for brief in briefs) / len(briefs)
    merged.typography = briefs[0].typography
    merged.typography.scale_ratio = sum(b.typography.scale_ratio for b in briefs) / len(briefs)
    merged.mood = list(dict.fromkeys(mood for brief in briefs for mood in brief.mood))[:6]
    merged.avoid = list(dict.fromkeys(item for brief in briefs for item in brief.avoid))[:6]
    merged.era = next((brief.era for brief in briefs if brief.era), "")
    merged.motion = next((brief.motion for brief in briefs if brief.motion), "")
    merged.notes = "; ".join(brief.notes for brief in briefs if brief.notes)[:400]
    return merged


def _most_common(values: Any) -> Any:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=lambda key: counts[key]) if counts else ""
