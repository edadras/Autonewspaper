"""Surface: the grain, the stock and the screen a design is printed on.

Flat colour looks like a computer made it. Paper has fibre, ink has grain, a
screened photograph has dots, and a risograph misregisters slightly - and a
design that has none of that reads as untouched however good its geometry is.

Every texture here is generated rather than shipped, so there is no library of
JPEGs to licence and a texture can be made at any size a piece needs. Each is
deterministic in its seed, so the same design rebuilt is the same design, and
each comes with the blend mode it is meant to be laid on with, because a paper
texture used as a normal layer is a grey rectangle.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

log = logging.getLogger(__name__)


class Texture(str, Enum):
    """The surfaces a design can be given."""

    PAPER = "paper"
    """Uncoated stock: long fibres and a soft blotch, laid on with multiply."""
    GRAIN = "grain"
    """Fine film grain, laid on with overlay."""
    NOISE = "noise"
    """Coarser digital noise, for a deliberately rough finish."""
    HALFTONE = "halftone"
    """The dot screen of a printed photograph."""
    SCANLINES = "scanlines"
    """Horizontal lines, for a broadcast or a screen-print feel."""
    VIGNETTE = "vignette"
    """Darkening towards the corners, which is where an eye is led from."""
    RISOGRAPH = "risograph"
    """Coarse grain with the mottle a duplicator leaves."""
    LINEN = "linen"
    """A woven cross-hatch, for a cover or an endpaper."""


#: How each texture is meant to be laid on. A paper texture set to normal is a
#: grey rectangle over the design; these are the modes that make it a surface.
BLEND_MODES: dict[Texture, str] = {
    Texture.PAPER: "multiply",
    Texture.GRAIN: "overlay",
    Texture.NOISE: "overlay",
    Texture.HALFTONE: "multiply",
    Texture.SCANLINES: "multiply",
    Texture.VIGNETTE: "multiply",
    Texture.RISOGRAPH: "multiply",
    Texture.LINEN: "multiply",
}

#: And how strongly, before anybody asks for something different. A texture
#: you can see is a texture that is too strong.
DEFAULT_OPACITY: dict[Texture, float] = {
    Texture.PAPER: 42.0,
    Texture.GRAIN: 26.0,
    Texture.NOISE: 22.0,
    Texture.HALFTONE: 30.0,
    Texture.SCANLINES: 18.0,
    Texture.VIGNETTE: 55.0,
    Texture.RISOGRAPH: 38.0,
    Texture.LINEN: 34.0,
}

#: Above this many pixels a texture is generated small and scaled up. Gaussian
#: noise over a six-metre hoarding at 300 dpi is seven hundred million pixels;
#: grain does not have to be generated at final resolution to look like grain.
MAX_GENERATED = 2600


@dataclass
class TextureSpec:
    """What a surface should look like."""

    kind: Texture
    width_px: int
    height_px: int
    color: str = "#000000"
    """What the texture is made of. Paper and grain are usually near-black
    laid on with multiply; a risograph is its own ink colour."""
    opacity: float = 0.0
    """Zero takes the default for this texture."""
    scale: float = 1.0
    """How coarse it is. Two is twice the grain size."""
    angle: float = 45.0
    """For a halftone screen or a linen weave."""
    seed: str = ""

    def cache_key(self) -> str:
        """A stable name, so the same surface is made once per edition."""
        parts = "|".join(
            str(value)
            for value in (
                self.kind.value,
                self.width_px,
                self.height_px,
                self.color,
                round(self.opacity, 2),
                round(self.scale, 3),
                round(self.angle, 1),
                self.seed,
            )
        )
        digest = hashlib.sha1(parts.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - a cache name
        return f"{self.kind.value}_{digest}"

    @property
    def blend_mode(self) -> str:
        """How this texture is meant to be laid on."""
        return BLEND_MODES.get(self.kind, "overlay")

    @property
    def strength(self) -> float:
        """The opacity to use."""
        return self.opacity or DEFAULT_OPACITY.get(self.kind, 30.0)


def _seed_of(spec: TextureSpec) -> int:
    """A deterministic seed, so a design rebuilt is the same design."""
    return int(hashlib.sha1(spec.cache_key().encode("utf-8")).hexdigest()[:8], 16)  # noqa: S324


def _noise_image(size: tuple[int, int], seed: int, *, smooth: bool = True) -> Image.Image:
    """Random grey, from a seed we control.

    Not :func:`PIL.Image.effect_noise`, which draws on Pillow's own generator
    and ignores :func:`random.seed` - so a design rebuilt would come back with
    different grain, and a texture cached under a key that no longer describes
    it. Averaging two draws pulls the distribution towards the middle, which
    is what grain looks like; a single uniform draw looks like static.
    """
    width, height = max(1, size[0]), max(1, size[1])
    count = width * height
    generator = random.Random(seed)  # noqa: S311 - texture, not security
    first = generator.randbytes(count)
    image = Image.frombytes("L", (width, height), first)
    if not smooth:
        return image
    second = Image.frombytes("L", (width, height), generator.randbytes(count))
    averaged = ImageChops.add(image, second, scale=2.0)
    image.close()
    second.close()
    return averaged


def _rgb(colour: str) -> tuple[int, int, int]:
    """A hex colour as three numbers."""
    text = colour.lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except (ValueError, IndexError):
        return (0, 0, 0)


def _working_size(spec: TextureSpec) -> tuple[int, int, float]:
    """The size to generate at, and how much it will be scaled up."""
    longest = max(spec.width_px, spec.height_px)
    if longest <= MAX_GENERATED:
        return (max(2, spec.width_px), max(2, spec.height_px), 1.0)
    factor = MAX_GENERATED / longest
    return (
        max(2, int(spec.width_px * factor)),
        max(2, int(spec.height_px * factor)),
        1.0 / factor,
    )


def make_texture(spec: TextureSpec) -> Image.Image:
    """Draw a surface, as an RGBA image ready to lay over a design.

    The colour carries in the pixels and the strength in the alpha, so the
    layer can be placed at a hundred per cent and still look like a surface
    rather than a filter.
    """
    width, height, upscale = _working_size(spec)
    builders = {
        Texture.PAPER: _paper,
        Texture.GRAIN: _grain,
        Texture.NOISE: _noise,
        Texture.HALFTONE: _halftone,
        Texture.SCANLINES: _scanlines,
        Texture.VIGNETTE: _vignette,
        Texture.RISOGRAPH: _risograph,
        Texture.LINEN: _linen,
    }
    build = builders.get(spec.kind, _grain)
    mask = build(spec, width, height, max(0.05, spec.scale / max(1.0, upscale)))

    if upscale > 1.0:
        mask = mask.resize((spec.width_px, spec.height_px), Image.Resampling.BILINEAR)

    faded = mask.point(lambda value: int(value * spec.strength / 100.0))
    texture = Image.new("RGBA", (spec.width_px, spec.height_px), (*_rgb(spec.color), 0))
    texture.putalpha(faded)
    mask.close()
    faded.close()
    return texture


# ------------------------------------------------------------- surfaces ----


def _paper(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """Uncoated stock: long fibres running one way, over a soft blotch."""
    seed = _seed_of(spec)
    coarse = max(2, int(24 * scale))
    # A blotch: noise at a fraction of the size, blown back up.
    blotch = _noise_image((max(2, width // coarse), max(2, height // coarse)), seed)
    blotch = blotch.resize((width, height), Image.Resampling.BICUBIC)

    # Fibres: noise stretched along the grain of the sheet.
    fibre = _noise_image((max(2, width // 2), max(2, height // max(2, int(6 * scale)))), seed + 1)
    fibre = fibre.resize((width, height), Image.Resampling.BILINEAR)
    fibre = fibre.filter(ImageFilter.GaussianBlur(0.6 * scale))

    mask = ImageChops.multiply(blotch, fibre)
    blotch.close()
    fibre.close()
    return ImageChops.invert(mask.filter(ImageFilter.GaussianBlur(0.3)))


def _grain(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """Fine film grain."""
    size = (max(2, int(width / max(1.0, scale))), max(2, int(height / max(1.0, scale))))
    grain = _noise_image(size, _seed_of(spec))
    if size != (width, height):
        grain = grain.resize((width, height), Image.Resampling.BILINEAR)
    return ImageChops.invert(grain)


def _noise(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """Coarser noise, for a deliberately rough finish."""
    coarse = max(1.0, 3.0 * scale)
    size = (max(2, int(width / coarse)), max(2, int(height / coarse)))
    rough = _noise_image(size, _seed_of(spec), smooth=False)
    return ImageChops.invert(rough.resize((width, height), Image.Resampling.NEAREST))


def _halftone(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """The dot screen of a printed photograph, at its own angle."""
    pitch = max(3, int(round(6 * scale)))
    radius = max(1.0, pitch * 0.34)
    # Drawn on a larger canvas so rotating it does not show its own corners.
    diagonal = int(math.hypot(width, height)) + pitch * 2
    plate = Image.new("L", (diagonal, diagonal), 0)
    draw = ImageDraw.Draw(plate)
    for y in range(0, diagonal, pitch):
        offset = (pitch // 2) if (y // pitch) % 2 else 0
        for x in range(-pitch, diagonal, pitch):
            draw.ellipse(
                [x + offset - radius, y - radius, x + offset + radius, y + radius], fill=255
            )
    rotated = plate.rotate(spec.angle, resample=Image.Resampling.BILINEAR)
    plate.close()
    left = (diagonal - width) // 2
    top = (diagonal - height) // 2
    cropped = rotated.crop((left, top, left + width, top + height))
    rotated.close()
    return cropped


def _scanlines(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """Horizontal lines, for a broadcast or screen-print feel."""
    pitch = max(2, int(round(4 * scale)))
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(0, height, pitch):
        draw.rectangle([0, y, width, y + max(1, pitch // 2) - 1], fill=255)
    return mask


def _vignette(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """Darkening towards the corners.

    Built as a small radial ramp and enlarged, rather than evaluated per
    pixel: a full-bleed poster is forty million pixels and the shape of a
    vignette survives being drawn at a two-hundredth of that.

    The ramp starts opaque and is lightened inwards, and the outermost circle
    reaches the *corners* rather than the edges - a vignette that leaves the
    corners untouched and darkens a ring in the middle of the picture is the
    opposite of a vignette.
    """
    small = 200
    ramp = Image.new("L", (small, small), 255)
    draw = ImageDraw.Draw(ramp)
    steps = 64
    # Past the inscribed circle, so the corners are covered by the ramp
    # instead of being left at the colour the mask started as.
    reach = small * 0.5 * math.sqrt(2) * max(0.25, 1.0 / max(0.25, scale))
    for step in range(steps):
        share = step / (steps - 1)
        radius = reach * (1.0 - share)
        value = int(255 * (1.0 - share) ** 1.8)
        draw.ellipse(
            [
                small / 2 - radius,
                small / 2 - radius,
                small / 2 + radius,
                small / 2 + radius,
            ],
            fill=value,
        )
    return ramp.resize((width, height), Image.Resampling.BICUBIC)


def _risograph(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """Coarse grain with the mottle a duplicator leaves."""
    grain = _noise(spec, width, height, scale * 1.6)
    coarse = max(2, int(40 * scale))
    mottle = _noise_image(
        (max(2, width // coarse), max(2, height // coarse)), _seed_of(spec) + 7
    ).resize((width, height), Image.Resampling.BICUBIC)
    out = ImageChops.multiply(grain, ImageChops.invert(mottle))
    grain.close()
    mottle.close()
    return out


def _linen(spec: TextureSpec, width: int, height: int, scale: float) -> Image.Image:
    """A woven cross-hatch.

    Drawn on a square large enough to survive being turned, then cropped, so
    the weave reaches the corners: rotating a mask in place leaves four bare
    triangles where the design shows through unweathered.
    """
    pitch = max(2, int(round(3 * scale)))
    diagonal = int(math.hypot(width, height)) + pitch * 2
    plate = Image.new("L", (diagonal, diagonal), 0)
    draw = ImageDraw.Draw(plate)
    for y in range(0, diagonal, pitch):
        draw.line([(0, y), (diagonal, y)], fill=200, width=1)
    for x in range(0, diagonal, pitch):
        draw.line([(x, 0), (x, diagonal)], fill=255, width=1)
    rotated = plate.rotate(spec.angle % 90, resample=Image.Resampling.BILINEAR)
    plate.close()
    left = (diagonal - width) // 2
    top = (diagonal - height) // 2
    cropped = rotated.crop((left, top, left + width, top + height))
    rotated.close()
    return cropped.filter(ImageFilter.GaussianBlur(0.4))


# --------------------------------------------------------------- factory ---


class TextureFactory:
    """Produces texture files, once each per edition."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._made: dict[str, Path] = {}

    def make(self, spec: TextureSpec) -> Path:
        """The file for *spec*, generating it if this edition has not."""
        key = spec.cache_key()
        cached = self._made.get(key)
        if cached is not None and cached.exists():
            return cached
        target = self.directory / f"{key}.png"
        if target.exists():
            self._made[key] = target
            return target
        texture = make_texture(spec)
        texture.save(target)
        texture.close()
        log.info(
            "Made a %s surface, %dx%d px -> %s",
            spec.kind.value,
            spec.width_px,
            spec.height_px,
            target.name,
        )
        self._made[key] = target
        return target

    def describe(self) -> dict[str, Any]:
        """What has been made for this edition."""
        return {"directory": str(self.directory), "surfaces": len(self._made)}
