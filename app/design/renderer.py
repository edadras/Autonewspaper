"""Rendering a design without Photoshop.

The same principle as the page renderer: everything the quality check
measures must be measurable on a machine with no Adobe installed, and the
operator must see the design before anything is built for real. The output is
a proof, not a press file - it is RGB raster with no colour management - and
the run says so, exactly as the built-in page renderer does.

Layer effects are approximated rather than reproduced: a drop shadow is a
blurred offset copy, a stroke is an outline, a gradient overlay is a real
gradient. Close enough to judge a composition by, and honest about being an
approximation.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.design.plan import Box, DesignPlan, Layer, LayerKind, ShapeKind
from app.vision.fonts import load_font
from app.vision.renderer import _text_kwargs, shape

log = logging.getLogger(__name__)


#: Points to pixels at the canvas resolution.
def _pt_to_px(size_pt: float, dpi: int) -> int:
    return max(1, int(round(size_pt / 72.0 * dpi)))


def _rgba(color: str, opacity: float = 100.0) -> tuple[int, int, int, int]:
    """Parse ``#rgb``/``#rrggbb`` into RGBA at *opacity* percent."""
    text = (color or "#000000").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 8:
        red, green, blue, alpha = (int(text[i : i + 2], 16) for i in (0, 2, 4, 6))
    elif len(text) == 6:
        red, green, blue = (int(text[i : i + 2], 16) for i in (0, 2, 4))
        alpha = 255
    else:
        red = green = blue = 0
        alpha = 255
    return (red, green, blue, int(alpha * max(0.0, min(100.0, opacity)) / 100.0))


class DesignRenderer:
    """Draws a :class:`DesignPlan` to an image."""

    def __init__(self, plan: DesignPlan, *, scale: float = 1.0) -> None:
        self.plan = plan
        self.scale = max(0.05, scale)

    # -------------------------------------------------------------- public
    def render(self, target: Path | str | None = None) -> Image.Image:
        """Draw the whole design; writes to *target* when one is given."""
        canvas = self.plan.canvas
        size = (
            max(1, int(canvas.width_px * self.scale)),
            max(1, int(canvas.height_px * self.scale)),
        )
        image = Image.new("RGBA", size, _rgba(canvas.background, 100))
        groups: dict[str, Layer] = {
            layer.name: layer for layer in self.plan.layers if layer.kind is LayerKind.GROUP
        }
        previous: Image.Image | None = None
        for layer in self.plan.ordered():
            if layer.kind is LayerKind.GROUP:
                continue
            drawn = self._draw_layer(layer, size)
            if drawn is None:
                continue
            if layer.clip_to_below and previous is not None:
                # Clipping to the layer below keeps only what overlaps it.
                drawn = self._clip(drawn, previous)
            group = groups.get(layer.group or "")
            if group is not None and group.opacity < 100:
                drawn = self._fade(drawn, group.opacity)
            image.alpha_composite(drawn)
            previous = drawn
        # A canvas asked to be transparent stays transparent. Flattening it
        # would put a black rectangle behind every piece of furniture, which
        # is exactly what a decorated box must not do.
        if _rgba(canvas.background, 100)[3] < 255:
            out = image
        else:
            out = Image.new("RGB", size, _rgba(canvas.background, 100)[:3])
            out.paste(image, (0, 0), image)
            image.close()
        if target is not None:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            if out.mode == "RGBA" and path.suffix.lower() in (".jpg", ".jpeg"):
                # JPEG cannot hold an alpha channel; say so rather than
                # writing a black-backed file the operator has to discover.
                raise ValueError(
                    f"'{path.name}' is transparent and cannot be written as JPEG; use PNG or TIFF"
                )
            out.save(path)
            log.info("Rendered '%s' to %s", self.plan.name or "design", path)
        return out

    def render_to(self, target: Path | str) -> Path:
        """Draw the design and return the path written."""
        image = self.render(target)
        image.close()
        return Path(target)

    def measure(self) -> list[dict[str, Any]]:
        """Where every layer's ink actually lands, for the quality check.

        Text is the reason this exists: a plan says a headline is 900 px wide,
        but what it needs depends on the font that is installed, so the box is
        measured from the rendered glyphs rather than assumed.
        """
        out: list[dict[str, Any]] = []
        for layer in self.plan.ordered():
            entry: dict[str, Any] = {
                "name": layer.name,
                "kind": layer.kind.value,
                "role": layer.role,
                "planned": layer.box.model_dump(),
            }
            if layer.is_text and layer.text.strip():
                entry.update(self._measure_text(layer))
            out.append(entry)
        return out

    # ------------------------------------------------------------ drawing
    def _draw_layer(self, layer: Layer, size: tuple[int, int]) -> Image.Image | None:
        if layer.kind is LayerKind.SHAPE:
            drawn = self._draw_shape(layer, size)
        elif layer.kind is LayerKind.TEXT:
            drawn = self._draw_text(layer, size)
        elif layer.kind is LayerKind.IMAGE:
            drawn = self._draw_image(layer, size)
        else:
            return None
        if drawn is None:
            return None
        if not layer.effects.is_empty():
            drawn = self._apply_effects(drawn, layer)
        if layer.rotation:
            drawn = drawn.rotate(-layer.rotation, resample=Image.Resampling.BICUBIC, expand=False)
        if layer.opacity < 100:
            drawn = self._fade(drawn, layer.opacity)
        return drawn

    def _box_px(self, box: Box) -> tuple[int, int, int, int]:
        return (
            int(round(box.x * self.scale)),
            int(round(box.y * self.scale)),
            int(round(box.right * self.scale)),
            int(round(box.bottom * self.scale)),
        )

    def _draw_shape(self, layer: Layer, size: tuple[int, int]) -> Image.Image:
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        fill = _rgba(layer.color, 100)
        left, top, right, bottom = self._box_px(layer.box)
        if layer.shape is ShapeKind.ELLIPSE:
            draw.ellipse([left, top, right, bottom], fill=fill)
        elif layer.shape is ShapeKind.ROUNDED:
            radius = max(0, int(layer.radius * self.scale))
            draw.rounded_rectangle([left, top, right, bottom], radius=radius, fill=fill)
        elif layer.shape is ShapeKind.POLYGON and len(layer.points) >= 3:
            draw.polygon(
                [(x * self.scale, y * self.scale) for x, y in layer.points],
                fill=fill,
            )
        else:
            draw.rectangle([left, top, right, bottom], fill=fill)
        return image

    def _draw_image(self, layer: Layer, size: tuple[int, int]) -> Image.Image | None:
        source = Path(layer.path) if layer.path else None
        left, top, right, bottom = self._box_px(layer.box)
        width, height = max(1, right - left), max(1, bottom - top)
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        if source is None or not source.exists():
            # A missing picture is drawn as a marked frame, so the design can
            # still be judged and the gap is obvious.
            draw = ImageDraw.Draw(image)
            draw.rectangle([left, top, right, bottom], fill=(222, 226, 230, 255))
            draw.line([left, top, right, bottom], fill=(150, 155, 160, 255), width=2)
            draw.line([left, bottom, right, top], fill=(150, 155, 160, 255), width=2)
            if layer.path:
                log.warning("Picture for layer '%s' is missing: %s", layer.name, layer.path)
            return image
        with Image.open(source) as opened:
            picture = opened.convert("RGBA")
        picture = self._fit(picture, width, height, layer.fit)
        image.paste(picture, (left + (width - picture.width) // 2, top + (height - picture.height) // 2))
        picture.close()
        return image

    @staticmethod
    def _fit(picture: Image.Image, width: int, height: int, mode: str) -> Image.Image:
        if mode == "stretch":
            return picture.resize((width, height), Image.Resampling.LANCZOS)
        ratio_w, ratio_h = width / picture.width, height / picture.height
        ratio = min(ratio_w, ratio_h) if mode == "contain" else max(ratio_w, ratio_h)
        resized = picture.resize(
            (max(1, int(picture.width * ratio)), max(1, int(picture.height * ratio))),
            Image.Resampling.LANCZOS,
        )
        if mode == "contain":
            return resized
        left = max(0, (resized.width - width) // 2)
        top = max(0, (resized.height - height) // 2)
        cropped = resized.crop((left, top, left + width, top + height))
        resized.close()
        return cropped

    def _font(self, layer: Layer) -> Any:
        size_px = max(1, int(round(_pt_to_px(layer.size_pt or 12, self.plan.canvas.dpi) * self.scale)))
        return load_font(
            layer.font or self.plan.fonts.get("body", ""),
            size_px,
            style=layer.font_style,
            script="arabic" if layer.direction == "rtl" else "latin",
            fallbacks=tuple(layer.fallback_fonts),
        )

    def _draw_text(self, layer: Layer, size: tuple[int, int]) -> Image.Image:
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        text = layer.text.upper() if layer.all_caps else layer.text
        if not text.strip():
            return image
        draw = ImageDraw.Draw(image)
        font = self._font(layer)
        left, top, right, _bottom = self._box_px(layer.box)
        max_width = max(1, right - left)
        leading = max(
            1,
            int(round(_pt_to_px(layer.leading_pt or layer.size_pt * 1.2, self.plan.canvas.dpi) * self.scale)),
        )
        colour = _rgba(layer.color, 100)
        kwargs = _text_kwargs(layer.direction)
        y = top
        for line in self._wrap(text, font, max_width, draw, layer.direction):
            rendered = shape(line, layer.direction)
            width = self._width(rendered, font, draw, layer.direction)
            if layer.alignment == "center":
                x = left + (max_width - width) / 2
            elif layer.alignment == "right" or (layer.direction == "rtl" and layer.alignment != "left"):
                x = right - width
            else:
                x = left
            draw.text((x, y), rendered, font=font, fill=colour, **kwargs)
            y += leading
        return image

    def _width(self, text: str, font: Any, draw: ImageDraw.ImageDraw, direction: str) -> float:
        if not text:
            return 0.0
        try:
            box = draw.textbbox((0, 0), text, font=font, **_text_kwargs(direction))
            return box[2] - box[0]
        except Exception:  # noqa: BLE001 - a font without metrics still has to render
            return len(text) * font.size * 0.5

    def _wrap(
        self, text: str, font: Any, max_width: int, draw: ImageDraw.ImageDraw, direction: str
    ) -> list[str]:
        lines: list[str] = []
        for paragraph in text.split("\n"):
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if self._width(shape(candidate, direction), font, draw, direction) <= max_width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    def _measure_text(self, layer: Layer) -> dict[str, Any]:
        scratch = Image.new("RGBA", (8, 8))
        draw = ImageDraw.Draw(scratch)
        font = self._font(layer)
        text = layer.text.upper() if layer.all_caps else layer.text
        max_width = max(1, int(round(layer.box.width * self.scale)))
        lines = self._wrap(text, font, max_width, draw, layer.direction)
        leading = max(
            1,
            int(round(_pt_to_px(layer.leading_pt or layer.size_pt * 1.2, self.plan.canvas.dpi) * self.scale)),
        )
        widest = max(
            (self._width(shape(line, layer.direction), font, draw, layer.direction) for line in lines),
            default=0.0,
        )
        scratch.close()
        needed = len(lines) * leading / max(1e-6, self.scale)
        available = max(1e-6, layer.box.height)
        return {
            "lines": len(lines),
            "measured_width": round(widest / max(1e-6, self.scale), 2),
            "measured_height": round(needed, 2),
            "overflow": round(max(0.0, (needed - available) / available), 4),
        }

    # ------------------------------------------------------------ effects
    @staticmethod
    def _fade(image: Image.Image, opacity: float) -> Image.Image:
        alpha = image.getchannel("A").point(lambda value: int(value * max(0.0, min(100.0, opacity)) / 100))
        faded = image.copy()
        faded.putalpha(alpha)
        return faded

    @staticmethod
    def _clip(image: Image.Image, below: Image.Image) -> Image.Image:
        """Keep only the part of *image* that overlaps the layer below it."""
        mask = below.getchannel("A")
        clipped = Image.new("RGBA", image.size, (0, 0, 0, 0))
        clipped.paste(image, (0, 0), mask)
        return clipped

    def _apply_effects(self, image: Image.Image, layer: Layer) -> Image.Image:
        effects = layer.effects
        out = image
        if effects.shadow:
            out = self._shadow(out, effects.shadow)
        if effects.glow:
            out = self._glow(out, effects.glow)
        if effects.stroke:
            out = self._stroke(out, effects.stroke)
        if effects.overlay:
            out = self._overlay(out, effects.overlay)
        if effects.gradient:
            out = self._gradient(out, effects.gradient, layer)
        return out

    def _shadow(self, image: Image.Image, spec: dict[str, Any]) -> Image.Image:
        angle = math.radians(float(spec.get("angle", 120)))
        distance = float(spec.get("distance", 8)) * self.scale
        blur = max(0.0, float(spec.get("size", 18)) * self.scale / 2)
        offset = (int(round(math.cos(angle) * distance)), int(round(-math.sin(angle) * distance)))
        colour = _rgba(str(spec.get("color", "#000000")), float(spec.get("opacity", 45)))
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        silhouette = Image.new("RGBA", image.size, colour)
        shadow.paste(silhouette, offset, image.getchannel("A"))
        silhouette.close()
        if blur:
            shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
        shadow.alpha_composite(image)
        return shadow

    def _glow(self, image: Image.Image, spec: dict[str, Any]) -> Image.Image:
        blur = max(1.0, float(spec.get("size", 20)) * self.scale / 2)
        colour = _rgba(str(spec.get("color", "#ffffff")), float(spec.get("opacity", 60)))
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        tint = Image.new("RGBA", image.size, colour)
        glow.paste(tint, (0, 0), image.getchannel("A"))
        tint.close()
        glow = glow.filter(ImageFilter.GaussianBlur(blur))
        glow.alpha_composite(image)
        return glow

    def _stroke(self, image: Image.Image, spec: dict[str, Any]) -> Image.Image:
        width = max(1, int(round(float(spec.get("size", 4)) * self.scale)))
        colour = _rgba(str(spec.get("color", "#000000")), float(spec.get("opacity", 100)))
        alpha = image.getchannel("A")
        grown = alpha.filter(ImageFilter.MaxFilter(width * 2 + 1))
        outline = Image.new("RGBA", image.size, (0, 0, 0, 0))
        tint = Image.new("RGBA", image.size, colour)
        outline.paste(tint, (0, 0), grown)
        tint.close()
        outline.alpha_composite(image)
        return outline

    @staticmethod
    def _overlay(image: Image.Image, spec: dict[str, Any]) -> Image.Image:
        colour = _rgba(str(spec.get("color", "#000000")), float(spec.get("opacity", 100)))
        tint = Image.new("RGBA", image.size, colour)
        out = image.copy()
        out.paste(tint, (0, 0), image.getchannel("A"))
        tint.close()
        return out

    def _gradient(self, image: Image.Image, spec: dict[str, Any], layer: Layer) -> Image.Image:
        """Fill the layer with a gradient along *angle*.

        Built as a one-pixel-wide ramp and then stretched and rotated, rather
        than evaluated per pixel: a full-resolution story frame is two million
        pixels, which takes thirteen seconds the naive way and a few
        milliseconds this way.
        """
        stops = spec.get("stops") or [
            {"color": spec.get("from", "#000000"), "location": 0, "opacity": 100},
            {"color": spec.get("to", "#ffffff"), "location": 100, "opacity": 100},
        ]
        ordered = sorted(stops, key=lambda stop: float(stop.get("location", 0)))
        angle = float(spec.get("angle", 90))
        width, height = image.size
        # The gradient belongs to the layer, not to the canvas: computing it
        # over the whole image leaves a layer smaller than the canvas seeing
        # only the middle slice of the ramp, which is why a caption bar had a
        # hard edge where it should have faded to nothing.
        box = layer.box
        box_left = box.x * self.scale
        box_top = box.y * self.scale
        box_width = max(1.0, box.width * self.scale)
        box_height = max(1.0, box.height * self.scale)
        # The ramp has to cover the diagonal so a rotation leaves no corner
        # unpainted.
        span = int(math.ceil(math.hypot(width, height))) or 1
        # The ramp has to be as long as the diagonal so a rotation leaves no
        # corner unpainted, but the gradient must *complete* over the box's
        # own extent along the gradient axis - otherwise the crop keeps only
        # the middle third of the ramp and the result is nearly flat.
        radians = math.radians(angle)
        extent = (abs(box_width * math.cos(radians)) + abs(box_height * math.sin(radians))) or 1.0
        # Where the box sits along the gradient axis, measured from the centre
        # of the canvas, so the ramp lines up with the layer after rotation.
        offset = (box_left + box_width / 2 - width / 2) * math.cos(radians) - (
            box_top + box_height / 2 - height / 2
        ) * math.sin(radians)
        start = (span - extent) / 2 + offset
        ramp = Image.new("RGBA", (span, 1))
        ramp.putdata([self._sample(ordered, max(0.0, min(1.0, (x - start) / extent))) for x in range(span)])
        gradient = ramp.resize((span, span), Image.Resampling.NEAREST)
        ramp.close()
        # Photoshop measures the angle anticlockwise from the positive x axis:
        # 0 runs left to right, 90 runs bottom to top. The preview has to agree
        # with it or the same plan comes out mirrored in the two engines.
        rotated = gradient.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False)
        gradient.close()
        left = (rotated.width - width) // 2
        top = (rotated.height - height) // 2
        cropped = rotated.crop((left, top, left + width, top + height))
        rotated.close()

        out = image.copy()
        faded = self._fade(cropped, float(spec.get("opacity", 100)))
        # The mask is the shape's own alpha multiplied by the gradient's:
        # pasting through the shape alone throws the per-stop opacity away, so
        # a caption bar meant to fade to nothing comes out a flat block.
        mask = ImageChops.multiply(image.getchannel("A"), faded.getchannel("A"))
        out.paste(faded, (0, 0), mask)
        # The result carries the gradient's transparency too, not only its
        # colour, or the layer would be opaque wherever the shape is.
        out.putalpha(mask)
        cropped.close()
        faded.close()
        mask.close()
        return out

    @staticmethod
    def _sample(stops: list[dict[str, Any]], position: float) -> tuple[int, int, int, int]:
        """Colour at *position* (0-1) along a stop list."""
        if not stops:
            return (0, 0, 0, 255)
        first = _rgba(str(stops[0].get("color", "#000000")), float(stops[0].get("opacity", 100)))
        if position <= float(stops[0].get("location", 0)) / 100:
            return first
        for left, right in zip(stops, stops[1:], strict=False):
            start = float(left.get("location", 0)) / 100
            end = float(right.get("location", 100)) / 100
            if start <= position <= end:
                span = max(1e-6, end - start)
                weight = (position - start) / span
                a = _rgba(str(left.get("color", "#000000")), float(left.get("opacity", 100)))
                b = _rgba(str(right.get("color", "#ffffff")), float(right.get("opacity", 100)))
                return tuple(int(a[i] + (b[i] - a[i]) * weight) for i in range(4))  # type: ignore[return-value]
        return _rgba(str(stops[-1].get("color", "#ffffff")), float(stops[-1].get("opacity", 100)))
