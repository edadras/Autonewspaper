"""Reading a reference the user attached.

Someone describing what they want will usually show you something instead:
a poster they like, a frame from a film, a competitor's post. This turns that
into a style brief a design can actually be built from.

The division of labour matters. Colour, contrast, brightness, where the
weight of the composition sits, whether the frame is busy or quiet, what the
dominant edges are - all of that is measurable, and it is measured, from the
pixels. Only the part that is genuinely judgement - what era it evokes, what
it is trying to say, what kind of typography would belong - goes to the
vision model, and it is asked to work *from* the measurements rather than
instead of them. A model that says "warm and earthy" about a picture measured
as blue gets overruled by the pixels.
"""

from __future__ import annotations

import colorsys
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

log = logging.getLogger(__name__)

#: Colours closer than this in RGB are treated as the same swatch.
_MERGE_DISTANCE = 42
#: How many pixels to reduce a reference to before counting colours.
_SAMPLE_SIDE = 220


@dataclass
class Swatch:
    """One colour taken from a reference."""

    hex: str
    share: float
    """Fraction of the frame this colour and its neighbours cover."""
    role: str = ""
    """``dominant``, ``secondary``, ``accent`` or ``neutral``."""

    @property
    def rgb(self) -> tuple[int, int, int]:
        """The colour as an RGB triple."""
        text = self.hex.lstrip("#")
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))

    @property
    def hsv(self) -> tuple[float, float, float]:
        """Hue, saturation and value, each 0-1."""
        r, g, b = self.rgb
        return colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)

    @property
    def luminance(self) -> float:
        """Perceived brightness, 0-1 (Rec. 709)."""
        r, g, b = (channel / 255 for channel in self.rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def is_neutral(self) -> bool:
        """Whether this is effectively a grey."""
        return self.hsv[1] < 0.12

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"hex": self.hex, "share": round(self.share, 4), "role": self.role}


@dataclass
class Measurements:
    """What the pixels say, before anyone interprets them."""

    width: int = 0
    height: int = 0
    aspect: float = 0.0
    palette: list[Swatch] = field(default_factory=list)
    brightness: float = 0.0
    """Mean luminance, 0-100."""
    contrast: float = 0.0
    """Standard deviation of luminance, 0-100."""
    saturation: float = 0.0
    """Mean saturation, 0-100."""
    warmth: float = 0.0
    """-100 (cold) to +100 (warm), from the red/blue balance."""
    busyness: float = 0.0
    """Edge density, 0-100: how much is going on in the frame."""
    weight_x: float = 0.5
    weight_y: float = 0.5
    """Where the ink sits, 0-1 across and down the frame."""
    empty_share: float = 0.0
    """Fraction of the frame that is nearly uniform - the breathing room."""
    is_monochrome: bool = False
    has_faces: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form, and what the vision model is shown."""
        return {
            "width": self.width,
            "height": self.height,
            "aspect": round(self.aspect, 3),
            "palette": [swatch.to_dict() for swatch in self.palette],
            "brightness": round(self.brightness, 1),
            "contrast": round(self.contrast, 1),
            "saturation": round(self.saturation, 1),
            "warmth": round(self.warmth, 1),
            "busyness": round(self.busyness, 1),
            "weight": {"x": round(self.weight_x, 3), "y": round(self.weight_y, 3)},
            "empty_share": round(self.empty_share, 3),
            "monochrome": self.is_monochrome,
            "faces": self.has_faces,
        }

    def describe(self) -> str:
        """A sentence a person would recognise, from the numbers alone."""
        parts: list[str] = []
        parts.append("dark" if self.brightness < 35 else "bright" if self.brightness > 68 else "mid-toned")
        if self.contrast > 32:
            parts.append("high contrast")
        elif self.contrast < 16:
            parts.append("flat")
        if self.is_monochrome:
            parts.append("nearly monochrome")
        elif self.saturation > 55:
            parts.append("saturated")
        elif self.saturation < 20:
            parts.append("desaturated")
        parts.append(
            "warm" if self.warmth > 12 else "cool" if self.warmth < -12 else "neutral in temperature"
        )
        parts.append("busy" if self.busyness > 26 else "sparse" if self.busyness < 9 else "moderately dense")
        if self.empty_share > 0.4:
            parts.append("with a lot of empty space")
        return ", ".join(parts)


def measure_image(path: Path | str, *, faces: bool = True) -> Measurements:
    """Measure a still reference."""
    source = Path(path)
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        measurements = Measurements(width=image.width, height=image.height)
        measurements.aspect = image.width / max(1, image.height)
        sample = image.copy()
        sample.thumbnail((_SAMPLE_SIDE, _SAMPLE_SIDE), Image.Resampling.LANCZOS)

        measurements.palette = extract_palette(sample)
        grey = sample.convert("L")
        stat = ImageStat.Stat(grey)
        measurements.brightness = round(stat.mean[0] / 255 * 100, 2)
        measurements.contrast = round(stat.stddev[0] / 255 * 100, 2)

        hsv = sample.convert("HSV")
        measurements.saturation = round(ImageStat.Stat(hsv.split()[1]).mean[0] / 255 * 100, 2)
        measurements.is_monochrome = measurements.saturation < 8

        red, _green, blue = (ImageStat.Stat(channel).mean[0] for channel in sample.split())
        measurements.warmth = round((red - blue) / 255 * 200, 2)

        edges = grey.filter(ImageFilter.FIND_EDGES)
        histogram = edges.histogram()
        total = sum(histogram) or 1
        measurements.busyness = round(sum(histogram[40:]) / total * 100, 2)

        measurements.weight_x, measurements.weight_y = _centre_of_mass(grey)
        measurements.empty_share = _empty_share(grey)
        hsv.close()
        sample.close()

    if faces:
        try:
            from app.utils import imaging

            measurements.has_faces = imaging.analyze(source).face_count
        except Exception as exc:  # noqa: BLE001 - a face count is a nicety
            log.debug("Face detection on the reference failed: %s", exc)
    return measurements


def extract_palette(image: Image.Image, count: int = 6) -> list[Swatch]:
    """The colours a reference is actually made of.

    Quantised and then merged: a quantiser alone returns six near-identical
    blues for a photograph of the sea, which is true and useless. Merging by
    distance gives the swatches a person would name.
    """
    reduced = image.convert("RGB").quantize(colors=48, method=Image.Quantize.MEDIANCUT)
    palette = reduced.getpalette() or []
    counts = sorted(reduced.getcolors() or [], key=lambda item: -item[0])
    total = sum(share for share, _index in counts) or 1

    merged: list[tuple[list[int], float]] = []
    for share, index in counts:
        rgb = palette[index * 3 : index * 3 + 3]
        if len(rgb) < 3:
            continue
        for existing in merged:
            if _distance(existing[0], rgb) < _MERGE_DISTANCE:
                weight = existing[1] + share / total
                # Weighted mean, so the swatch drifts towards the bigger area.
                for channel in range(3):
                    existing[0][channel] = int(
                        (existing[0][channel] * existing[1] + rgb[channel] * (share / total)) / weight
                    )
                existing_index = merged.index(existing)
                merged[existing_index] = (existing[0], weight)
                break
        else:
            merged.append((list(rgb), share / total))
    merged.sort(key=lambda item: -item[1])
    reduced.close()

    swatches = [
        Swatch(hex=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}", share=share) for rgb, share in merged[:count]
    ]
    return assign_roles(swatches)


def assign_roles(swatches: list[Swatch]) -> list[Swatch]:
    """Name each swatch by the job it does in the composition."""
    if not swatches:
        return swatches
    coloured = [s for s in swatches if not s.is_neutral()]
    for index, swatch in enumerate(swatches):
        if swatch.is_neutral():
            swatch.role = "neutral"
        elif index == 0 or (coloured and swatch is coloured[0]):
            swatch.role = "dominant"
        elif coloured and swatch is coloured[-1] and len(coloured) > 2:
            swatch.role = "accent"
        else:
            swatch.role = "secondary"
    # An accent is the least common strongly-saturated colour; without one a
    # design has nothing to punctuate with.
    if coloured and not any(s.role == "accent" for s in swatches):
        strongest = max(coloured, key=lambda s: s.hsv[1])
        if strongest.role != "dominant":
            strongest.role = "accent"
    return swatches


def _distance(left: list[int], right: list[int]) -> float:
    return math.sqrt(sum((left[i] - right[i]) ** 2 for i in range(3)))


def _centre_of_mass(grey: Image.Image) -> tuple[float, float]:
    """Where the dark pixels sit, as fractions across and down."""
    width, height = grey.size
    pixels = grey.load()
    weight_x = weight_y = total = 0.0
    step = max(1, min(width, height) // 80)
    for y in range(0, height, step):
        for x in range(0, width, step):
            ink = (255 - pixels[x, y]) / 255
            weight_x += x * ink
            weight_y += y * ink
            total += ink
    if total <= 0:
        return (0.5, 0.5)
    return (weight_x / total / max(1, width), weight_y / total / max(1, height))


def _empty_share(grey: Image.Image) -> float:
    """Fraction of the frame that is nearly uniform."""
    width, height = grey.size
    cells_x, cells_y = 12, 12
    cell_w, cell_h = max(1, width // cells_x), max(1, height // cells_y)
    quiet = 0
    for row in range(cells_y):
        for column in range(cells_x):
            box = (column * cell_w, row * cell_h, (column + 1) * cell_w, (row + 1) * cell_h)
            cell = grey.crop(box)
            if ImageStat.Stat(cell).stddev[0] < 12:
                quiet += 1
            cell.close()
    return quiet / (cells_x * cells_y)
