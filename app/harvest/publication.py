"""The design system a publication turns out to have.

One page is a sample; a run of pages is the specification. This module takes
the per-page measurements and reduces them to the things a template is made
of - one sheet, one set of margins, one grid, one palette, one type scale -
by agreement across the pages rather than by trusting whichever one happened
to be first.

What comes out is a :class:`TemplateSpec` the studio can lay pages out on and
a set of :class:`FurnitureSpec` pieces that redraw the paper's own boxes, so
the boxes are copied rather than reinvented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.design.furniture import FurnitureSpec
from app.harvest.analysis import PageMeasurements, Panel, Rule, TypeBand, type_scale
from app.templates.schema import (
    ColorSpec,
    GridSpec,
    MarginSpec,
    ParagraphStyleSpec,
    TemplateSpec,
)

log = logging.getLogger(__name__)

#: The roles a type scale falls into, largest first. A paper sets more sizes
#: than this, but these are the ones a template has to name.
TYPE_ROLES = ("masthead", "headline", "subheadline", "lead", "body", "caption")


@dataclass
class HarvestedPage:
    """One page's measurements, kept so the operator can check the reading."""

    measurements: PageMeasurements

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return self.measurements.to_dict()


@dataclass
class DesignSystem:
    """What a publication is made of."""

    name: str = ""
    source: str = ""
    page_width_mm: float = 0.0
    page_height_mm: float = 0.0
    margin_top_mm: float = 0.0
    margin_bottom_mm: float = 0.0
    margin_inside_mm: float = 0.0
    margin_outside_mm: float = 0.0
    columns: int = 6
    gutter_mm: float = 4.0
    paper: str = "#ffffff"
    ink: str = "#111111"
    accent: str = "#c2410c"
    palette: list[str] = field(default_factory=list)
    sizes_pt: list[float] = field(default_factory=list)
    rule_weights_pt: list[float] = field(default_factory=list)
    furniture: list[FurnitureSpec] = field(default_factory=list)
    pages: list[HarvestedPage] = field(default_factory=list)
    confidence: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ reading
    @property
    def column_width_mm(self) -> float:
        """The measure this paper sets to."""
        live = self.page_width_mm - self.margin_inside_mm - self.margin_outside_mm
        if self.columns <= 1:
            return live
        return (live - self.gutter_mm * (self.columns - 1)) / self.columns

    def role_sizes(self) -> dict[str, float]:
        """The type scale, given the names a template uses.

        The largest size is the masthead only if it is much larger than the
        next; a paper whose front page carries no masthead in the sample
        should not have its biggest headline called one.
        """
        sizes = sorted(self.sizes_pt, reverse=True)
        if not sizes:
            return {}
        roles = list(TYPE_ROLES)
        if len(sizes) > 1 and sizes[0] < sizes[1] * 1.6:
            roles.remove("masthead")
        out: dict[str, float] = {}
        for role, size in zip(roles, sizes, strict=False):
            out[role] = size
        # Body copy is the size most of the page is set in, which is the
        # smallest common one rather than the fifth largest.
        if "body" not in out and sizes:
            out["body"] = sizes[-1]
        return out

    def describe(self) -> str:
        """One paragraph an operator can check against the paper in their hand."""
        parts = [
            f"{self.page_width_mm:.0f} x {self.page_height_mm:.0f} mm",
            f"margins {self.margin_top_mm:.0f}/{self.margin_bottom_mm:.0f}/"
            f"{self.margin_inside_mm:.0f}/{self.margin_outside_mm:.0f} mm",
            f"{self.columns} columns at {self.column_width_mm:.1f} mm, "
            f"{self.gutter_mm:.1f} mm gutter",
        ]
        if self.sizes_pt:
            parts.append("type " + ", ".join(f"{size:g}pt" for size in sorted(self.sizes_pt)))
        if self.furniture:
            kinds = sorted({piece.kind.value for piece in self.furniture})
            parts.append(f"{len(self.furniture)} box(es): {', '.join(kinds)}")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "name": self.name,
            "source": self.source,
            "page_width_mm": self.page_width_mm,
            "page_height_mm": self.page_height_mm,
            "margins_mm": {
                "top": round(self.margin_top_mm, 1),
                "bottom": round(self.margin_bottom_mm, 1),
                "inside": round(self.margin_inside_mm, 1),
                "outside": round(self.margin_outside_mm, 1),
            },
            "columns": self.columns,
            "gutter_mm": round(self.gutter_mm, 1),
            "column_width_mm": round(self.column_width_mm, 1),
            "paper": self.paper,
            "ink": self.ink,
            "accent": self.accent,
            "palette": self.palette,
            "sizes_pt": self.sizes_pt,
            "roles": self.role_sizes(),
            "rule_weights_pt": self.rule_weights_pt,
            "furniture": [
                {
                    "kind": piece.kind.value,
                    "width_mm": round(piece.width_mm, 1),
                    "height_mm": round(piece.height_mm, 1),
                    "color": piece.color,
                    "accent": piece.accent,
                    "corner_mm": piece.corner_mm,
                    "rule_pt": piece.rule_pt,
                }
                for piece in self.furniture
            ],
            "confidence": {key: round(value, 2) for key, value in self.confidence.items()},
            "notes": self.notes,
            "pages": [page.to_dict() for page in self.pages],
        }

    def save(self, path: Path | str) -> Path:
        """Write the system out as JSON."""
        import json

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    # ----------------------------------------------------------- template
    def to_template(
        self,
        *,
        template_id: str = "",
        name: str = "",
        language: str = "fa",
        base: TemplateSpec | None = None,
    ) -> TemplateSpec:
        """Turn the measurements into a template the studio can work on.

        The sheet, the margins, the grid, the colours and the type sizes come
        from the publication. Everything a measurement cannot reach - which
        font, how a style is justified, what the PDF presets are - is left as
        the *base* template had it, because guessing at those from pixels
        would be inventing rather than harvesting.
        """
        spec = (base or TemplateSpec(id="harvested", name="Harvested")).model_copy(deep=True)
        spec.id = template_id or f"harvested_{_slug(self.name or self.source or 'publication')}"
        spec.name = name or self.name or f"Harvested from {self.source}"
        spec.description = f"Measured from {self.source}. {self.describe()}"
        spec.language = language  # type: ignore[assignment]
        spec.direction = "rtl" if language in ("fa", "ar") else "ltr"  # type: ignore[assignment]
        spec.page_width_mm = round(self.page_width_mm, 2)
        spec.page_height_mm = round(self.page_height_mm, 2)
        spec.margins = MarginSpec(
            top=round(self.margin_top_mm, 1),
            bottom=round(self.margin_bottom_mm, 1),
            inside=round(self.margin_inside_mm, 1),
            outside=round(self.margin_outside_mm, 1),
        )
        spec.grid = GridSpec(
            columns=max(1, min(24, self.columns)),
            gutter_mm=round(max(0.0, min(30.0, self.gutter_mm)), 2),
            baseline_mm=spec.grid.baseline_mm,
            rows=spec.grid.rows,
        )
        spec.colors = _colours(self.palette, self.paper, self.ink, self.accent) or spec.colors
        spec.paragraph_styles = _styles(spec.paragraph_styles, self.role_sizes())
        # The master pages of the base are sized for its own sheet.
        spec.master_pages = []
        spec.indesign_template_path = None
        spec.meta = {
            **spec.meta,
            "harvested_from": self.source,
            "harvest_confidence": self.confidence,
            "harvest_notes": self.notes,
        }
        return spec


def _slug(text: str, limit: int = 40) -> str:
    """A stable id from a publication's name."""
    cleaned = "".join(character if character.isalnum() else "_" for character in text.lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:limit] or "publication"


def _colours(palette: list[str], paper: str, ink: str, accent: str) -> list[ColorSpec]:
    """The publication's inks, named the way a template names them."""
    from app.design.fidelity import parse_color

    out = [
        ColorSpec(name="Black", cyan=0, magenta=0, yellow=0, black=100),
        ColorSpec(name="Paper", **_cmyk(parse_color(paper))),
    ]
    seen = {paper.lower(), ink.lower()}
    for index, colour in enumerate(palette):
        if colour.lower() in seen:
            continue
        seen.add(colour.lower())
        rgb = parse_color(colour)
        if rgb is None:
            continue
        name = "Accent" if colour.lower() == accent.lower() else f"Harvested {index + 1}"
        out.append(ColorSpec(name=name, **_cmyk(rgb)))
    return out


def _cmyk(rgb: tuple[int, int, int] | None) -> dict[str, float]:
    """A rough CMYK for a screen colour.

    Rough on purpose: a real conversion needs the paper's own profile, which
    a PDF of a printed page does not carry. It is enough to put a swatch on
    the palette with the right hue, and the operator sets the values their
    press wants.
    """
    if rgb is None:
        return {"cyan": 0.0, "magenta": 0.0, "yellow": 0.0, "black": 100.0}
    red, green, blue = (band / 255 for band in rgb)
    black = 1 - max(red, green, blue)
    if black >= 0.999:
        return {"cyan": 0.0, "magenta": 0.0, "yellow": 0.0, "black": 100.0}
    return {
        "cyan": round((1 - red - black) / (1 - black) * 100, 1),
        "magenta": round((1 - green - black) / (1 - black) * 100, 1),
        "yellow": round((1 - blue - black) / (1 - black) * 100, 1),
        "black": round(black * 100, 1),
    }


def _styles(
    existing: list[ParagraphStyleSpec], roles: dict[str, float]
) -> list[ParagraphStyleSpec]:
    """Put the measured sizes onto the base template's styles.

    Only the size and the leading are replaced. A measurement can see how big
    the type is; it cannot see which font it is, and pretending otherwise
    would put a font nobody has into the template.
    """
    if not roles:
        return existing
    out: list[ParagraphStyleSpec] = []
    for style in existing:
        size = roles.get(style.id)
        if size is None:
            out.append(style)
            continue
        updated = style.model_copy(deep=True)
        ratio = (style.leading_pt / style.size_pt) if style.size_pt else 1.25
        updated.size_pt = round(size, 1)
        updated.leading_pt = round(size * ratio, 1)
        updated.min_size_pt = min(updated.min_size_pt, round(size * 0.8, 1))
        updated.max_size_pt = max(updated.max_size_pt, round(size * 1.3, 1))
        out.append(updated)
    return out


__all__ = [
    "redraw_furniture",
    "scaled_furniture",
    "DesignSystem",
    "HarvestedPage",
    "PageMeasurements",
    "Panel",
    "Rule",
    "TypeBand",
    "type_scale",
]


def redraw_furniture(system: DesignSystem, factory: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    """Draw the publication's own boxes again, in Photoshop.

    This is the point of measuring them: the paper's tint panels and ruled
    boxes come back as files a page can place, at whatever size the new page
    needs, drawn by the same factory that draws the studio's own furniture.
    A piece that cannot be drawn does not stop the rest.
    """
    out: list[dict[str, Any]] = []
    for piece in system.furniture[:limit]:
        try:
            path = factory.make(piece)
        except Exception as exc:  # noqa: BLE001 - one box is not the whole set
            log.warning("The %s could not be redrawn: %s", piece.kind.value, exc)
            out.append({"kind": piece.kind.value, "drawn": False, "error": str(exc)})
            continue
        out.append(
            {
                "kind": piece.kind.value,
                "drawn": True,
                "path": str(path),
                "width_mm": round(piece.width_mm, 1),
                "height_mm": round(piece.height_mm, 1),
                "color": piece.color,
            }
        )
    return out


def scaled_furniture(
    system: DesignSystem, kind: str, *, width_mm: float, height_mm: float, dpi: int = 300
) -> FurnitureSpec:
    """One of the publication's boxes, at the size a new page needs it.

    The colours, the corner and the rule weight are the paper's own; only the
    dimensions change, which is what makes this copying a box rather than
    copying a picture of one.
    """
    from app.design.furniture import Furniture

    match = next((piece for piece in system.furniture if piece.kind.value == kind), None)
    if match is None:
        raise ValueError(
            f"This publication has no {kind}; it has "
            + (", ".join(sorted({piece.kind.value for piece in system.furniture})) or "no boxes")
        )
    return FurnitureSpec(
        kind=Furniture(kind),
        width_mm=float(width_mm),
        height_mm=float(height_mm),
        dpi=dpi,
        color=match.color,
        accent=match.accent,
        ink=match.ink,
        corner_mm=match.corner_mm,
        rule_pt=match.rule_pt,
        opacity=match.opacity,
        direction=match.direction,
        seed=match.seed,
    )
