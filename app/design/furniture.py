"""The decorated boxes a designer makes by hand.

A newspaper page is not only type in columns. It has tinted sidebar panels,
ruled boxes around a fact list, corner ornaments on a feature, a plate behind
a pull quote, a border on a photograph, a bar behind a section head. On a real
desk somebody draws those in Photoshop and places them in InDesign, because
Photoshop can do things to an edge that a page-layout program cannot.

That is exactly what this does. Each piece of furniture is a design in its own
right - built as a :class:`DesignPlan`, so it goes through the same builder,
the same renderer and the same fidelity check as anything else - produced at
the page's print resolution with transparency where it needs it, and handed
back as a file the layout engine places behind or around a frame.

It works with no Photoshop installed: the built-in renderer draws the same
plan. The piece is then a raster either way, which is what it would be anyway.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from app.design.plan import Box, Canvas, DesignPlan, Effects, Layer, LayerKind, ShapeKind
from app.design.renderer import DesignRenderer

log = logging.getLogger(__name__)

#: Furniture is drawn a little larger than the frame it sits behind, so a
#: shadow or a glow is not clipped at the edge.
BLEED_PX = 24


class Furniture(str, Enum):
    """The pieces a page can ask for."""

    TINT_PANEL = "tint_panel"
    """A flat or graded panel behind a sidebar."""
    RULED_BOX = "ruled_box"
    """A box with a keyline, the way a fact list is set off."""
    SHADOW_CARD = "shadow_card"
    """A card that lifts off the page."""
    CORNER_ORNAMENT = "corner_ornament"
    """Rules that turn the corners of a feature box."""
    SECTION_BAR = "section_bar"
    """A solid bar a section name sits in."""
    QUOTE_PLATE = "quote_plate"
    """A plate behind a pull quote, with an oversized mark."""
    PHOTO_FRAME = "photo_frame"
    """A border and a drop shadow for a photograph."""
    DROP_CAP_PLATE = "drop_cap_plate"
    """A tinted square behind an opening capital."""
    GRADIENT_WASH = "gradient_wash"
    """A soft wash used under a masthead or a full-bleed picture."""
    SECTION_TAB = "section_tab"
    """The tab a section name sits in at the head of an inside page."""
    PAGE_BADGE = "page_badge"
    """The disc or rounded square a page number is reversed out of."""
    EDGE_ARC = "edge_arc"
    """A half-disc bleeding off the trim, used to mark a margin."""
    DOTTED_RULE = "dotted_rule"
    """The broken rule that separates one teaser from the next."""
    CAPTION_BAR = "caption_bar"
    """A translucent bar laid over the foot of a photograph."""
    FLAG_MARKER = "flag_marker"
    """The small triangle that flags a kicker or a section head."""
    FOOTER_RULE = "footer_rule"
    """The rule that closes a page, with room for the folio."""


@dataclass
class FurnitureSpec:
    """What a piece of furniture should look like."""

    kind: Furniture
    width_mm: float
    height_mm: float
    dpi: int = 300
    color: str = "#f2f0eb"
    accent: str = "#c2410c"
    ink: str = "#111111"
    corner_mm: float = 0.0
    """Corner radius; zero is a square corner."""
    rule_pt: float = 0.75
    opacity: float = 100.0
    direction: str = "rtl"
    seed: str = ""
    """Anything that should make two otherwise identical pieces differ."""

    def cache_key(self) -> str:
        """A stable name, so the same piece is drawn once per edition."""
        parts = "|".join(
            str(value)
            for value in (
                self.kind.value,
                round(self.width_mm, 2),
                round(self.height_mm, 2),
                self.dpi,
                self.color,
                self.accent,
                self.ink,
                self.corner_mm,
                self.rule_pt,
                self.opacity,
                self.direction,
                self.seed,
            )
        )
        digest = hashlib.sha1(parts.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - a cache name
        return f"{self.kind.value}_{digest}"

    @property
    def width_px(self) -> int:
        """Width in pixels at this piece's resolution."""
        return max(1, int(round(self.width_mm / 25.4 * self.dpi)))

    @property
    def height_px(self) -> int:
        """Height in pixels at this piece's resolution."""
        return max(1, int(round(self.height_mm / 25.4 * self.dpi)))


def _mm(value: float, dpi: int) -> float:
    return value / 25.4 * dpi


def _pt(value: float, dpi: int) -> float:
    return value / 72.0 * dpi


class FurnitureDesigner:
    """Builds the plan for one piece of furniture."""

    def plan(self, spec: FurnitureSpec) -> DesignPlan:
        """A design plan for *spec*, transparent where it should be."""
        pad = BLEED_PX
        canvas = Canvas(
            width_px=spec.width_px + pad * 2,
            height_px=spec.height_px + pad * 2,
            dpi=spec.dpi,
            mode="rgb",
            background="#00000000",
            format_name=spec.kind.value,
            medium="print",
        )
        plan = DesignPlan(name=spec.cache_key(), canvas=canvas)
        plan.meta["furniture"] = spec.kind.value
        plan.meta["inset_px"] = pad
        body = Box(x=pad, y=pad, width=spec.width_px, height=spec.height_px)

        builder = {
            Furniture.TINT_PANEL: self._tint_panel,
            Furniture.RULED_BOX: self._ruled_box,
            Furniture.SHADOW_CARD: self._shadow_card,
            Furniture.CORNER_ORNAMENT: self._corner_ornament,
            Furniture.SECTION_BAR: self._section_bar,
            Furniture.QUOTE_PLATE: self._quote_plate,
            Furniture.PHOTO_FRAME: self._photo_frame,
            Furniture.DROP_CAP_PLATE: self._drop_cap_plate,
            Furniture.GRADIENT_WASH: self._gradient_wash,
            Furniture.SECTION_TAB: self._section_tab,
            Furniture.PAGE_BADGE: self._page_badge,
            Furniture.EDGE_ARC: self._edge_arc,
            Furniture.DOTTED_RULE: self._dotted_rule,
            Furniture.CAPTION_BAR: self._caption_bar,
            Furniture.FLAG_MARKER: self._flag_marker,
            Furniture.FOOTER_RULE: self._footer_rule,
        }[spec.kind]
        builder(plan, spec, body)
        return plan

    # ------------------------------------------------------------- pieces
    def _tint_panel(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        radius = _mm(spec.corner_mm, spec.dpi)
        plan.add(
            Layer(
                name="panel",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.ROUNDED if radius > 0 else ShapeKind.RECTANGLE,
                radius=radius,
                box=body,
                color=spec.color,
                opacity=spec.opacity,
            )
        )
        # A hairline of the accent along the reading edge is what stops a tint
        # panel looking like a printing accident.
        edge = max(2.0, _pt(spec.rule_pt * 3, spec.dpi))
        x = body.right - edge if spec.direction == "rtl" else body.x
        plan.add(
            Layer(
                name="edge",
                kind=LayerKind.SHAPE,
                box=Box(x=x, y=body.y, width=edge, height=body.height),
                color=spec.accent,
            )
        )

    def _ruled_box(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        weight = max(1.0, _pt(spec.rule_pt, spec.dpi))
        radius = _mm(spec.corner_mm, spec.dpi)
        plan.add(
            Layer(
                name="fill",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.ROUNDED if radius > 0 else ShapeKind.RECTANGLE,
                radius=radius,
                box=body,
                color=spec.color,
                opacity=spec.opacity,
                effects=Effects(stroke={"color": spec.ink, "size": weight, "opacity": 100}),
            )
        )

    def _shadow_card(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        radius = _mm(spec.corner_mm or 2.0, spec.dpi)
        plan.add(
            Layer(
                name="card",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.ROUNDED,
                radius=radius,
                box=body,
                color=spec.color,
                opacity=spec.opacity,
                effects=Effects(
                    shadow={
                        "color": "#000000",
                        "opacity": 28,
                        "angle": 270,
                        "distance": max(2, BLEED_PX // 4),
                        "size": BLEED_PX,
                    }
                ),
            )
        )

    def _corner_ornament(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        weight = max(1.5, _pt(spec.rule_pt * 2, spec.dpi))
        arm = min(body.width, body.height) * 0.22
        for index, (x, y, w, h) in enumerate(
            [
                (body.x, body.y, arm, weight),
                (body.x, body.y, weight, arm),
                (body.right - arm, body.y, arm, weight),
                (body.right - weight, body.y, weight, arm),
                (body.x, body.bottom - weight, arm, weight),
                (body.x, body.bottom - arm, weight, arm),
                (body.right - arm, body.bottom - weight, arm, weight),
                (body.right - weight, body.bottom - arm, weight, arm),
            ]
        ):
            plan.add(
                Layer(
                    name=f"arm_{index}",
                    kind=LayerKind.SHAPE,
                    box=Box(x=x, y=y, width=w, height=h),
                    color=spec.ink,
                    opacity=spec.opacity,
                )
            )

    def _section_bar(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        plan.add(
            Layer(
                name="bar",
                kind=LayerKind.SHAPE,
                box=body,
                color=spec.accent,
                opacity=spec.opacity,
            )
        )
        # A thin rule below, separated by the bar's own height, is the detail
        # that reads as a designed section head rather than a coloured block.
        gap = body.height * 0.28
        plan.add(
            Layer(
                name="underline",
                kind=LayerKind.SHAPE,
                box=Box(
                    x=body.x,
                    y=body.bottom + gap,
                    width=body.width,
                    height=max(1.0, _pt(spec.rule_pt, spec.dpi)),
                ),
                color=spec.ink,
            )
        )

    def _quote_plate(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        plan.add(
            Layer(
                name="plate",
                kind=LayerKind.SHAPE,
                box=body,
                color=spec.color,
                opacity=spec.opacity,
            )
        )
        mark_size = min(body.height * 0.62, body.width * 0.3)
        # The quotation mark sits at the opening corner, which flips with the
        # reading direction.
        x = body.right - mark_size * 0.9 if spec.direction == "rtl" else body.x + mark_size * 0.1
        plan.add(
            Layer(
                name="mark",
                kind=LayerKind.TEXT,
                box=Box(x=x, y=body.y - mark_size * 0.15, width=mark_size, height=mark_size),
                text="”" if spec.direction == "rtl" else "“",
                size_pt=mark_size / spec.dpi * 72,
                color=spec.accent,
                opacity=45,
                alignment="center",
                direction=spec.direction,
            )
        )
        rule = max(1.0, _pt(spec.rule_pt * 2, spec.dpi))
        plan.add(
            Layer(
                name="rule",
                kind=LayerKind.SHAPE,
                box=Box(x=body.x, y=body.y, width=body.width, height=rule),
                color=spec.accent,
            )
        )

    def _photo_frame(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        weight = max(2.0, _pt(spec.rule_pt * 2, spec.dpi))
        plan.add(
            Layer(
                name="mount",
                kind=LayerKind.SHAPE,
                box=body,
                color=spec.color,
                opacity=spec.opacity,
                effects=Effects(
                    stroke={"color": spec.ink, "size": weight, "opacity": 100},
                    shadow={
                        "color": "#000000",
                        "opacity": 22,
                        "angle": 270,
                        "distance": max(2, BLEED_PX // 5),
                        "size": BLEED_PX,
                    },
                ),
            )
        )

    def _drop_cap_plate(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        plan.add(
            Layer(
                name="plate",
                kind=LayerKind.SHAPE,
                box=body,
                color=spec.accent,
                opacity=spec.opacity,
            )
        )

    def _gradient_wash(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        plan.add(
            Layer(
                name="wash",
                kind=LayerKind.SHAPE,
                box=body,
                color=spec.color,
                opacity=spec.opacity,
                effects=Effects(
                    gradient={
                        "stops": [
                            {"color": spec.color, "location": 0, "opacity": 100},
                            {"color": spec.color, "location": 55, "opacity": 55},
                            {"color": spec.color, "location": 100, "opacity": 0},
                        ],
                        "angle": 90,
                    }
                ),
            )
        )

    def _section_tab(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """A tab with square shoulders and a rounded foot, as a section head."""
        radius = _mm(spec.corner_mm or 8.0, spec.dpi)
        plan.add(
            Layer(
                name="tab",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.ROUNDED,
                radius=radius,
                box=body,
                color=spec.accent,
                opacity=spec.opacity,
            )
        )
        # Square off the top so the tab reads as hanging from the trim rather
        # than floating: a rounded rectangle with its head covered.
        plan.add(
            Layer(
                name="shoulders",
                kind=LayerKind.SHAPE,
                box=Box(x=body.x, y=body.y, width=body.width, height=radius),
                color=spec.accent,
                opacity=spec.opacity,
            )
        )

    def _page_badge(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """The disc a folio is reversed out of."""
        side = min(body.width, body.height)
        square = Box(
            x=body.x + (body.width - side) / 2,
            y=body.y + (body.height - side) / 2,
            width=side,
            height=side,
        )
        rounded = spec.corner_mm > 0
        plan.add(
            Layer(
                name="badge",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.ROUNDED if rounded else ShapeKind.ELLIPSE,
                radius=_mm(spec.corner_mm, spec.dpi),
                box=square,
                color=spec.accent,
                opacity=spec.opacity,
            )
        )

    def _edge_arc(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """A half-disc that runs off the trim.

        Drawn as a full circle with half of it outside the piece, so whichever
        edge it is placed against it bleeds correctly.
        """
        diameter = body.height
        x = body.x - diameter / 2 if spec.direction == "rtl" else body.right - diameter / 2
        plan.add(
            Layer(
                name="arc",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.ELLIPSE,
                box=Box(x=x, y=body.y, width=diameter, height=diameter),
                color=spec.accent,
                opacity=spec.opacity,
            )
        )

    def _dotted_rule(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """A broken rule, as between one teaser and the next."""
        weight = max(1.0, _pt(spec.rule_pt, spec.dpi))
        dash = weight * 4
        gap = weight * 3
        x = body.x
        index = 0
        while x < body.right - 1:
            width = min(dash, body.right - x)
            plan.add(
                Layer(
                    name=f"dash_{index}",
                    kind=LayerKind.SHAPE,
                    box=Box(x=x, y=body.y, width=width, height=weight),
                    color=spec.ink,
                    opacity=spec.opacity,
                )
            )
            x += dash + gap
            index += 1

    def _caption_bar(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """A translucent bar over the foot of a photograph.

        Graded rather than flat: a hard-edged block over a picture looks
        pasted on, and a gradient to nothing is how a caption is actually set
        over an image.
        """
        plan.add(
            Layer(
                name="bar",
                kind=LayerKind.SHAPE,
                box=body,
                color=spec.ink,
                opacity=spec.opacity if spec.opacity < 100 else 78,
                effects=Effects(
                    gradient={
                        "stops": [
                            {"color": spec.ink, "location": 0, "opacity": 0},
                            {"color": spec.ink, "location": 45, "opacity": 85},
                            {"color": spec.ink, "location": 100, "opacity": 95},
                        ],
                        # 270 runs top to bottom, so the first stop - nothing
                        # at all - is at the top of the bar.
                        "angle": 270,
                    }
                ),
            )
        )

    def _flag_marker(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """The small triangle that flags a kicker."""
        if spec.direction == "rtl":
            points = [
                (body.right, body.y),
                (body.right, body.bottom),
                (body.x, body.y),
            ]
        else:
            points = [
                (body.x, body.y),
                (body.x, body.bottom),
                (body.right, body.y),
            ]
        plan.add(
            Layer(
                name="flag",
                kind=LayerKind.SHAPE,
                shape=ShapeKind.POLYGON,
                points=points,
                box=body,
                color=spec.accent,
                opacity=spec.opacity,
            )
        )

    def _footer_rule(self, plan: DesignPlan, spec: FurnitureSpec, body: Box) -> None:
        """The rule that closes a page."""
        weight = max(1.0, _pt(spec.rule_pt, spec.dpi))
        plan.add(
            Layer(
                name="rule",
                kind=LayerKind.SHAPE,
                box=Box(x=body.x, y=body.y, width=body.width, height=weight),
                color=spec.accent,
                opacity=spec.opacity,
            )
        )


class FurnitureFactory:
    """Produces furniture files, in Photoshop when it is there.

    The same plan either way, so a page laid out on a machine without
    Photoshop and one laid out with it differ in the quality of the edge, not
    in what is on the page.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        photoshop: Any = None,
        designer: FurnitureDesigner | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.photoshop = photoshop
        self.designer = designer or FurnitureDesigner()
        self._made: dict[str, Path] = {}

    def make(self, spec: FurnitureSpec) -> Path:
        """The file for *spec*, drawing it if this edition has not already."""
        key = spec.cache_key()
        cached = self._made.get(key)
        if cached is not None and cached.exists():
            return cached
        target = self.directory / f"{key}.png"
        if target.exists():
            self._made[key] = target
            return target

        plan = self.designer.plan(spec)
        engine = "builtin"
        if self.photoshop is not None:
            try:
                self.photoshop.build_design(plan, export=target, flatten=False)
                engine = "photoshop"
            except Exception as exc:  # noqa: BLE001 - the page still needs its furniture
                log.warning(
                    "Photoshop could not draw the %s; the built-in renderer did: %s",
                    spec.kind.value,
                    exc,
                )
        if engine == "builtin" or not target.exists():
            DesignRenderer(plan).render_to(target)
            engine = "builtin"
        log.info("Drew %s (%s) -> %s", spec.kind.value, engine, target.name)
        self._made[key] = target
        return target

    def placement(self, spec: FurnitureSpec, frame: Box, dpi: int) -> Box:
        """Where the piece goes on the page, in millimetres.

        The piece is drawn larger than its frame so an edge effect is not
        clipped, so it is placed back out by the same amount - otherwise every
        panel would sit a couple of millimetres small and nobody would know
        why.
        """
        inset_mm = BLEED_PX / dpi * 25.4
        return Box(
            x=frame.x - inset_mm,
            y=frame.y - inset_mm,
            width=frame.width + inset_mm * 2,
            height=frame.height + inset_mm * 2,
        )

    def describe(self) -> dict[str, Any]:
        """What has been drawn for this edition."""
        return {
            "directory": str(self.directory),
            "pieces": len(self._made),
            "engine": "photoshop" if self.photoshop is not None else "builtin",
        }
