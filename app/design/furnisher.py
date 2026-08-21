"""Putting the drawn furniture onto the page.

The pieces are designed in Photoshop and then placed in InDesign, which is
how a real desk works. Because a piece is a file, placing it needs nothing
new from the InDesign side: it becomes an ordinary picture frame sitting
behind the type, built by the same call as any photograph.

What decides where the pieces go is editorial, not decorative. A sidebar gets
a keyline box because it is an aside; a section head gets a tab because the
reader needs to know which section they are in; a photograph gets a caption
bar only when there is a caption to put in it. Furniture that decorates
nothing is what makes a page look designed-at rather than designed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.design.furniture import Furniture, FurnitureFactory, FurnitureSpec
from app.design.plan import Box
from app.models.schemas import ElementSpec, ElementType, PageLayout, Rect
from app.templates.schema import TemplateSpec

log = logging.getLogger(__name__)

#: Furniture sits behind the type it decorates. The layout engine gives text
#: a z-index at or above zero, so anything below that is safely underneath.
BACKGROUND_Z = -50

#: A frame smaller than this in either direction is not worth decorating.
MIN_DECORATED_MM = 18.0


@dataclass
class FurnishingStyle:
    """How a publication dresses its pages.

    Every value has a plain default, so a template that says nothing still
    gets a page that looks considered rather than bare.
    """

    accent: str = "#1f4e9c"
    secondary: str = "#e0aa2e"
    ink: str = "#111111"
    paper: str = "#f4f2ec"
    tint: str = "#eef1f6"
    corner_mm: float = 0.0
    rule_pt: float = 0.75

    sidebar_panel: bool = True
    """A tint or keyline behind an aside."""
    sidebar_style: Furniture = Furniture.RULED_BOX
    section_tab: bool = True
    folio_badge: bool = True
    edge_arcs: bool = True
    """The half-discs that mark the outer margin."""
    caption_bars: bool = True
    quote_plates: bool = True
    photo_frames: bool = False
    """Off by default: a border round every photograph dates a page fast."""
    kicker_flags: bool = True
    footer_rule: bool = True

    @classmethod
    def from_template(cls, template: TemplateSpec) -> FurnishingStyle:
        """Take the colours a template already declares."""
        style = cls()
        accent = template.color("Accent") or template.color("Section")
        if accent is not None and getattr(accent, "hex", ""):
            style.accent = accent.hex
        secondary = template.color("Highlight") or template.color("Secondary")
        if secondary is not None and getattr(secondary, "hex", ""):
            style.secondary = secondary.hex
        rules = template.layout_rules
        style.rule_pt = max(0.25, min(3.0, getattr(rules, "rule_weight_pt", style.rule_pt)))
        return style

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "accent": self.accent,
            "secondary": self.secondary,
            "ink": self.ink,
            "paper": self.paper,
            "tint": self.tint,
            "pieces": {
                name: getattr(self, name)
                for name in (
                    "sidebar_panel",
                    "section_tab",
                    "folio_badge",
                    "edge_arcs",
                    "caption_bars",
                    "quote_plates",
                    "photo_frames",
                    "kicker_flags",
                    "footer_rule",
                )
            },
        }


@dataclass
class Furnishing:
    """One piece placed on a page, and why."""

    element_id: str
    kind: Furniture
    path: str
    rect: Rect
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "element_id": self.element_id,
            "kind": self.kind.value,
            "path": self.path,
            "rect": self.rect.model_dump(),
            "reason": self.reason,
        }


@dataclass
class FurnishingResult:
    """What was added to a page."""

    added: list[Furnishing] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "added": [item.to_dict() for item in self.added],
            "skipped": self.skipped,
        }


class PageFurnisher:
    """Adds the drawn furniture to a laid-out page."""

    def __init__(
        self,
        factory: FurnitureFactory,
        *,
        style: FurnishingStyle | None = None,
        dpi: int = 300,
    ) -> None:
        self.factory = factory
        self.style = style or FurnishingStyle()
        self.dpi = dpi

    def furnish(self, page: PageLayout, *, direction: str = "rtl") -> FurnishingResult:
        """Place furniture behind the elements that call for it.

        The page is changed in place, because the furniture has to reach
        InDesign through the same plan the type does.
        """
        result = FurnishingResult()
        style = self.style

        for element in list(page.elements):
            if element.type is ElementType.SIDEBAR and style.sidebar_panel:
                self._behind(page, element, style.sidebar_style, result, direction, "a sidebar is an aside")
            elif element.type is ElementType.QUOTE and style.quote_plates:
                self._behind(page, element, Furniture.QUOTE_PLATE, result, direction, "a pull quote")
            elif element.type is ElementType.KICKER and style.kicker_flags:
                self._flag(page, element, result, direction)
            elif element.type is ElementType.IMAGE and style.photo_frames:
                self._behind(page, element, Furniture.PHOTO_FRAME, result, direction, "a photograph")
            elif element.type is ElementType.CAPTION and style.caption_bars:
                self._caption(page, element, result, direction)

        if style.section_tab and page.section:
            self._section_tab(page, result, direction)
        if style.folio_badge:
            self._folio_badge(page, result, direction)
        if style.edge_arcs:
            self._edge_arcs(page, result, direction)
        if style.footer_rule:
            self._footer_rule(page, result, direction)

        if result.added:
            log.info(
                "Page %d: placed %d piece(s) of furniture (%s)",
                page.index,
                len(result.added),
                ", ".join(sorted({item.kind.value for item in result.added})),
            )
        return result

    # ------------------------------------------------------------- pieces
    def _behind(
        self,
        page: PageLayout,
        element: ElementSpec,
        kind: Furniture,
        result: FurnishingResult,
        direction: str,
        reason: str,
    ) -> None:
        if element.rect.width < MIN_DECORATED_MM or element.rect.height < MIN_DECORATED_MM:
            result.skipped.append(f"{element.id}: too small to decorate")
            return
        spec = self._spec(kind, element.rect.width, element.rect.height, direction)
        self._place(page, spec, element.rect, result, reason, anchor=element.id)

    def _flag(self, page: PageLayout, element: ElementSpec, result: FurnishingResult, direction: str) -> None:
        side = min(6.0, element.rect.height)
        # The flag sits in the margin ahead of the kicker, on the reading side.
        x = element.rect.right + 1.5 if direction == "rtl" else element.rect.x - side - 1.5
        rect = Rect(x=x, y=element.rect.y, width=side, height=side)
        if rect.x < 0 or rect.right > page.width_mm:
            result.skipped.append(f"{element.id}: no room in the margin for a flag")
            return
        spec = self._spec(Furniture.FLAG_MARKER, side, side, direction, accent=self.style.secondary)
        self._place(page, spec, rect, result, "a kicker is flagged", anchor=element.id)

    def _caption(
        self, page: PageLayout, element: ElementSpec, result: FurnishingResult, direction: str
    ) -> None:
        if not element.text.strip():
            result.skipped.append(f"{element.id}: no caption to put in a bar")
            return
        spec = self._spec(Furniture.CAPTION_BAR, element.rect.width, element.rect.height, direction)
        self._place(page, spec, element.rect, result, "a caption over a picture", anchor=element.id)

    def _section_tab(self, page: PageLayout, result: FurnishingResult, direction: str) -> None:
        head = next(
            (e for e in page.elements if e.type is ElementType.KICKER and e.meta.get("master")),
            None,
        )
        width, height = 30.0, 16.0
        x = page.width_mm - page.margin_outside_mm - width if direction == "rtl" else page.margin_outside_mm
        rect = Rect(x=x, y=0.0, width=width, height=height)
        if head is not None:
            rect.y = max(0.0, head.rect.y - height - 2)
        spec = self._spec(
            Furniture.SECTION_TAB, width, height, direction, accent=self.style.secondary, corner_mm=4.0
        )
        self._place(page, spec, rect, result, f"the '{page.section}' section", anchor="section")

    def _folio_badge(self, page: PageLayout, result: FurnishingResult, direction: str) -> None:
        folio = next((e for e in page.elements if e.type is ElementType.FOLIO), None)
        side = 12.0
        y = folio.rect.y - 3 if folio is not None else page.height_mm - page.margin_bottom_mm
        x = page.margin_inside_mm if direction == "rtl" else page.width_mm - page.margin_outside_mm - side
        rect = Rect(x=x, y=min(y, page.height_mm - side), width=side, height=side)
        spec = self._spec(Furniture.PAGE_BADGE, side, side, direction)
        self._place(page, spec, rect, result, "the folio", anchor="folio")

    def _edge_arcs(self, page: PageLayout, result: FurnishingResult, direction: str) -> None:
        content = page.content_rect
        diameter = 14.0
        for index, fraction in enumerate((0.34, 0.55)):
            colour = self.style.accent if index else self.style.secondary
            y = content.y + content.height * fraction
            for side, x in (
                ("outer", page.width_mm - diameter / 2),
                ("inner", -diameter / 2),
            ):
                rect = Rect(x=x, y=y, width=diameter, height=diameter)
                spec = self._spec(
                    Furniture.EDGE_ARC,
                    diameter,
                    diameter,
                    "rtl" if side == "inner" else "ltr",
                    accent=colour,
                )
                self._place(page, spec, rect, result, "a mark in the margin", anchor=f"arc_{index}_{side}")

    def _footer_rule(self, page: PageLayout, result: FurnishingResult, direction: str) -> None:
        content = page.content_rect
        folio = next((e for e in page.elements if e.type is ElementType.FOLIO), None)
        y = folio.rect.y - 2.5 if folio is not None else page.height_mm - page.margin_bottom_mm
        rect = Rect(x=content.x, y=y, width=content.width, height=1.0)
        spec = self._spec(Furniture.FOOTER_RULE, content.width, 1.0, direction, rule_pt=1.0)
        self._place(page, spec, rect, result, "the page is closed", anchor="footer")

    # ------------------------------------------------------------ plumbing
    def _spec(
        self,
        kind: Furniture,
        width_mm: float,
        height_mm: float,
        direction: str,
        *,
        accent: str | None = None,
        corner_mm: float | None = None,
        rule_pt: float | None = None,
    ) -> FurnitureSpec:
        style = self.style
        return FurnitureSpec(
            kind=kind,
            width_mm=round(max(1.0, width_mm), 2),
            height_mm=round(max(1.0, height_mm), 2),
            dpi=self.dpi,
            color=style.tint,
            accent=accent or style.accent,
            ink=style.ink,
            corner_mm=style.corner_mm if corner_mm is None else corner_mm,
            rule_pt=style.rule_pt if rule_pt is None else rule_pt,
            direction=direction,
        )

    def _place(
        self,
        page: PageLayout,
        spec: FurnitureSpec,
        frame: Rect,
        result: FurnishingResult,
        reason: str,
        *,
        anchor: str,
    ) -> None:
        """Draw the piece and add it to the page as a picture frame."""
        try:
            path = self.factory.make(spec)
        except Exception as exc:  # noqa: BLE001 - a page without a panel is still a page
            log.warning("Could not draw the %s for %s: %s", spec.kind.value, anchor, exc)
            result.skipped.append(f"{anchor}: {exc}")
            return
        placed = self.factory.placement(
            spec, Box(x=frame.x, y=frame.y, width=frame.width, height=frame.height), self.dpi
        )
        rect = Rect(x=placed.x, y=placed.y, width=placed.width, height=placed.height)
        element_id = f"furniture_{spec.kind.value}_{anchor}"
        page.elements.append(
            ElementSpec(
                id=element_id,
                type=ElementType.IMAGE,
                rect=rect,
                z_index=BACKGROUND_Z,
                image_path=str(path),
                fit_mode="none",
                locked=True,
                style_id="furniture",
                text_wrap_mm=0.0,
                meta={
                    "furniture": spec.kind.value,
                    "decorates": anchor,
                    "reason": reason,
                    # Furniture is not content: the quality check must not
                    # measure it as a picture that needs to be sharp.
                    "decorative": True,
                },
            )
        )
        result.added.append(
            Furnishing(element_id=element_id, kind=spec.kind, path=str(path), rect=rect, reason=reason)
        )
