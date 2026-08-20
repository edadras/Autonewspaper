"""Built-in page renderer.

Renders a :class:`~app.models.schemas.PageLayout` to an image. It has two
jobs:

* give the operator an immediate preview while planning, before InDesign is
  touched at all;
* act as the fallback output path when InDesign is not available on the
  machine, so a run still produces previews and a PDF.

InDesign remains the source of truth for the finished document
(specification §52): when it is available, QA analyses *its* export, not this
one, and the exported PDF comes from InDesign.

Persian and Arabic text is shaped correctly in either of two ways: when
Pillow is built with Raqm (HarfBuzz + FriBiDi) the logical text is handed
straight to Pillow, which shapes and reorders it itself; otherwise the text is
pre-shaped with ``arabic_reshaper`` and reordered with ``python-bidi``. Doing
both would reverse the text twice, so the renderer picks exactly one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.models.schemas import ElementSpec, ElementType, LayoutPlan, PageLayout, Rect
from app.templates.schema import TemplateSpec
from app.utils import text as T
from app.utils.units import mm_to_px
from app.vision.fonts import load_font

log = logging.getLogger(__name__)

from PIL import features as _pil_features

try:  # pragma: no cover - optional
    import arabic_reshaper  # type: ignore
    from bidi.algorithm import get_display  # type: ignore

    _RESHAPER = True
except Exception:  # pragma: no cover
    arabic_reshaper = None  # type: ignore
    get_display = None  # type: ignore
    _RESHAPER = False

try:  # pragma: no cover - depends on the Pillow build
    _RAQM = bool(_pil_features.check("raqm"))
except Exception:  # pragma: no cover
    _RAQM = False

#: ``True`` when right-to-left text can be drawn correctly by some means.
_SHAPING = _RAQM or _RESHAPER

PAPER = (255, 255, 255)
INK = (20, 20, 20)
GUIDE = (206, 214, 224)
IMAGE_FILL = (222, 226, 230)
IMAGE_EDGE = (170, 176, 182)


def shape(text: str, direction: str) -> str:
    """Pre-shape *text* for drawing, when Pillow cannot do it itself.

    With a Raqm-enabled Pillow this is a no-op: Pillow applies HarfBuzz
    shaping and the FriBiDi reordering during ``draw.text``, and pre-shaping
    here would reverse the string a second time.
    """
    if direction != "rtl" or not text or _RAQM or not _RESHAPER:
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))  # type: ignore[misc]
    except Exception:  # noqa: BLE001 - never fail a preview over shaping
        return text


def _text_kwargs(direction: str) -> dict[str, Any]:
    """Extra ``draw.text``/``textbbox`` arguments for the given direction."""
    if _RAQM and direction == "rtl":
        return {"direction": "rtl", "language": "fa"}
    return {}


@dataclass
class RenderResult:
    """Outcome of rendering one page."""

    path: Path
    width_px: int
    height_px: int
    dpi: int
    shaped: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "path": str(self.path),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "shaped": self.shaped,
            "warnings": self.warnings,
        }


class PreviewRenderer:
    """Draws layout plans as raster images."""

    def __init__(self, template: TemplateSpec, dpi: int = 110, *, show_guides: bool = False) -> None:
        self.template = template
        self.dpi = max(36, min(600, dpi))
        self.show_guides = show_guides
        self.direction = template.direction
        self.script = "arabic" if template.language in ("fa", "ar") else "latin"
        self._colors = {color.name: color.to_rgb() for color in template.colors}

    # ------------------------------------------------------------ helpers
    def px(self, mm: float) -> int:
        """Millimetres to pixels at the render resolution."""
        return int(round(mm_to_px(mm, self.dpi)))

    def color(self, name: str | None, default: tuple[int, int, int] = INK) -> tuple[int, int, int]:
        """Resolve a template colour name."""
        if not name:
            return default
        return self._colors.get(name, default)

    def font_for(self, element: ElementSpec):
        """PIL font for an element's resolved typography."""
        typography = element.typography
        if typography is None:
            return load_font("Arial", self.px(3.5), "Regular", self.script)
        size_px = max(4, int(round(typography.size_pt * self.dpi / 72.0)))
        return load_font(
            typography.font_family,
            size_px,
            typography.font_style or "Regular",
            self.script,
            tuple(typography.fallback_fonts),
        )

    # ------------------------------------------------------------- drawing
    def render_page(self, page: PageLayout, target: Path | str) -> RenderResult:
        """Render one page to *target*."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        width_px, height_px = self.px(page.width_mm), self.px(page.height_mm)
        image = Image.new("RGB", (max(1, width_px), max(1, height_px)), PAPER)
        draw = ImageDraw.Draw(image)
        warnings: list[str] = []

        if self.show_guides:
            self._draw_guides(draw, page)

        for element in sorted(page.elements, key=lambda e: e.z_index):
            try:
                if element.is_image:
                    self._draw_image(image, draw, element, warnings)
                elif element.type is ElementType.RULE:
                    self._draw_rule(draw, element)
                else:
                    self._draw_text(draw, element, page)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the preview
                warnings.append(f"{element.frame_name}: {exc}")
                log.debug("Preview element %s failed: %s", element.id, exc)

        image.save(target, dpi=(self.dpi, self.dpi))
        if not _SHAPING and self.script == "arabic":
            warnings.append(
                "No Arabic shaping is available (Pillow lacks Raqm and arabic_reshaper is "
                "not installed); preview text is unshaped."
            )
        return RenderResult(
            path=target,
            width_px=image.width,
            height_px=image.height,
            dpi=self.dpi,
            shaped=_SHAPING or self.script != "arabic",
            warnings=warnings,
        )

    def _draw_guides(self, draw: ImageDraw.ImageDraw, page: PageLayout) -> None:
        content = page.content_rect
        draw.rectangle(
            [self.px(content.x), self.px(content.y), self.px(content.right), self.px(content.bottom)],
            outline=GUIDE,
            width=1,
        )
        for column in range(1, page.columns):
            x = self.px(content.x + column * (page.column_width() + page.gutter_mm) - page.gutter_mm / 2)
            draw.line([x, self.px(content.y), x, self.px(content.bottom)], fill=GUIDE, width=1)

    def _draw_rule(self, draw: ImageDraw.ImageDraw, element: ElementSpec) -> None:
        rect = element.rect
        thickness = max(1, self.px(max(0.2, element.rect.height)))
        y = self.px(rect.y)
        draw.rectangle(
            [self.px(rect.x), y, self.px(rect.right), y + thickness],
            fill=self.color(element.fill_color, INK),
        )

    def _draw_image(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        element: ElementSpec,
        warnings: list[str],
    ) -> None:
        rect = element.rect
        box = (self.px(rect.x), self.px(rect.y), self.px(rect.right), self.px(rect.bottom))
        width, height = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        source = Path(element.image_path) if element.image_path else None
        if source and source.exists():
            try:
                from PIL import ImageOps

                with Image.open(source) as picture:
                    picture = ImageOps.exif_transpose(picture).convert("RGB")
                    fitted = ImageOps.fit(
                        picture, (width, height), Image.Resampling.LANCZOS, centering=(0.5, 0.42)
                    )
                canvas.paste(fitted, (box[0], box[1]))
                return
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{element.frame_name}: cannot draw image ({exc})")
        draw.rectangle(box, fill=IMAGE_FILL, outline=IMAGE_EDGE, width=1)
        draw.line([box[0], box[1], box[2], box[3]], fill=IMAGE_EDGE, width=1)
        draw.line([box[0], box[3], box[2], box[1]], fill=IMAGE_EDGE, width=1)
        if not source:
            warnings.append(f"{element.frame_name}: no image assigned")

    def _draw_text(self, draw: ImageDraw.ImageDraw, element: ElementSpec, page: PageLayout) -> None:
        if not element.text.strip():
            return
        typography = element.typography
        font = self.font_for(element)
        color = self.color(typography.color if typography else None, INK)
        rect = element.rect
        columns = max(1, typography.columns if typography else 1)
        gutter = typography.column_gutter_mm if typography else page.gutter_mm
        column_width = (rect.width - gutter * (columns - 1)) / columns
        leading_px = self.px(
            (typography.leading_pt if typography else 12.0) * 25.4 / 72.0
        )
        alignment = typography.alignment if typography else "left"
        direction = typography.direction if typography else self.direction

        lines = self._wrap(element.text, font, self.px(column_width), draw)
        lines_per_column = max(1, int(self.px(rect.height) / max(1, leading_px)))

        for index, line in enumerate(lines):
            column = index // lines_per_column
            if column >= columns:
                break
            row = index % lines_per_column
            # RTL pages fill their columns from the right.
            visual_column = (columns - 1 - column) if direction == "rtl" else column
            column_x = rect.x + visual_column * (column_width + gutter)
            y = self.px(rect.y) + row * leading_px
            drawn = shape(line, direction)
            text_width = self._measure(drawn, font, draw, direction)
            if alignment == "center":
                x = self.px(column_x + column_width / 2) - text_width // 2
            elif alignment in ("right", "justify", "justify_last_right") and direction == "rtl":
                x = self.px(column_x + column_width) - text_width
            elif alignment == "right":
                x = self.px(column_x + column_width) - text_width
            else:
                x = self.px(column_x)
            draw.text((x, y), drawn, font=font, fill=color, **_text_kwargs(direction))

    def _measure(
        self, text: str, font: Any, draw: ImageDraw.ImageDraw, direction: str | None = None
    ) -> int:
        try:
            box = draw.textbbox((0, 0), text, font=font, **_text_kwargs(direction or self.direction))
            return int(box[2] - box[0])
        except Exception:  # noqa: BLE001
            return int(len(text) * 6)

    def _wrap(self, text: str, font: Any, max_width_px: int, draw: ImageDraw.ImageDraw) -> list[str]:
        """Greedy word wrap on the *logical* text (before shaping)."""
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                lines.append("")
                continue
            words = paragraph.split(" ")
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                measured = self._measure(shape(candidate, self.direction), font, draw)
                if measured <= max_width_px or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        return lines

    # ------------------------------------------------------------- edition
    def render_plan(self, plan: LayoutPlan, directory: Path | str, prefix: str = "page") -> list[RenderResult]:
        """Render every page of a plan into *directory*."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        results = []
        for page in plan.pages:
            results.append(
                self.render_page(page, directory / f"{prefix}_{page.index:03d}.png")
            )
        return results

    def render_pdf(self, plan: LayoutPlan, target: Path | str, dpi: int | None = None) -> Path:
        """Write a multi-page PDF from the internal renderer.

        This is the fallback output when InDesign is unavailable; the run
        result records that the PDF did not come from InDesign.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        original_dpi = self.dpi
        if dpi:
            self.dpi = dpi
        try:
            images: list[Image.Image] = []
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                for page in plan.pages:
                    result = self.render_page(page, Path(tmp) / f"p{page.index:03d}.png")
                    images.append(Image.open(result.path).convert("RGB"))
                if not images:
                    raise ValueError("The layout plan has no pages")
                images[0].save(
                    target,
                    "PDF",
                    resolution=float(self.dpi),
                    save_all=True,
                    append_images=images[1:],
                )
                for image in images:
                    image.close()
        finally:
            self.dpi = original_dpi
        log.info("Rendered %d page(s) to %s with the built-in renderer", len(plan.pages), target)
        return target

    def thumbnail(self, page: PageLayout, target: Path | str, max_side: int = 360) -> Path:
        """Render a small thumbnail of a page for the UI."""
        target = Path(target)
        original = self.dpi
        self.dpi = max(24, int(max_side / (page.height_mm / 25.4)))
        try:
            self.render_page(page, target)
        finally:
            self.dpi = original
        return target


def shaping_available() -> bool:
    """Whether right-to-left shaping is available (reported by Diagnostics)."""
    return _SHAPING


def shaping_engine() -> str:
    """Which shaping engine the renderer will use."""
    if _RAQM:
        return "raqm"
    if _RESHAPER:
        return "arabic_reshaper+bidi"
    return "none"
