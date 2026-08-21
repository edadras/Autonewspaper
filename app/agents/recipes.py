"""What a specialist does when no model is driving it.

Specification §57 bounds every agent loop, and the loop needs something to do
on each turn. With a cloud model configured that is the model's own choice of
tool; without one - and whenever the model's reply cannot be parsed - it is
one of these recipes.

They are not placeholders. Each is the sequence of tool calls a designer would
make for that kind of job, with the proportions, the type scale and the colour
relationships written down rather than guessed, so a machine with no model at
all still produces work somebody would put their name to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.autonomous import AgentStep
from app.creative.style import StyleBrief, readable_on, shift
from app.design.textures import Texture

log = logging.getLogger(__name__)

#: A step of a recipe: the tool to call and the arguments to call it with.
Call = tuple[str, dict[str, Any]]


@dataclass
class Palette:
    """The four colours a piece is built from."""

    ground: str = "#0f172a"
    ink: str = "#f8fafc"
    accent: str = "#c2410c"
    paper: str = "#f2f0eb"

    @classmethod
    def from_brief(cls, brief: StyleBrief | None) -> Palette:
        """Take the palette from a reference, keeping it readable.

        The foreground is only inherited if it actually reads against the
        background: a reference photograph can easily suggest two colours a
        designer would never set one on the other.
        """
        if brief is None:
            return cls()
        ground = brief.background or "#0f172a"
        ink = brief.foreground or readable_on(ground)
        if _contrast(ground, ink) < 4.5:
            ink = readable_on(ground)
        accent = brief.accent or "#c2410c"
        if _contrast(ground, accent) < 2.0:
            accent = shift(accent, lighten=0.35, saturate=0.2)
        paper = brief.swatch("paper") or shift(ground, lighten=0.92)
        return cls(ground=ground, ink=ink, accent=accent, paper=paper)


def _contrast(left: str, right: str) -> float:
    from app.creative.style import contrast_ratio

    try:
        return contrast_ratio(left, right)
    except Exception:  # noqa: BLE001 - a malformed colour is not worth failing over
        return 21.0


@dataclass
class Recipe:
    """A named sequence of tool calls, played one step at a time."""

    name: str
    calls: list[Call] = field(default_factory=list)

    def planner(self) -> Callable[[list[AgentStep]], AgentStep | None]:
        """A fallback planner that walks this recipe.

        A call that has already been made - successfully or not - is not
        repeated: a recipe is a plan, and a plan that loops is not one.
        """

        def plan(steps: list[AgentStep]) -> AgentStep | None:
            done = {(step.tool, _key(step.arguments)) for step in steps if step.result is not None}
            for tool, arguments in self.calls:
                if (tool, _key(arguments)) not in done:
                    return AgentStep(index=0, thought=f"{self.name}: {tool}", tool=tool, arguments=dict(arguments))
            return AgentStep(index=0, thought=f"{self.name} is complete", done=True)

        return plan


def _key(arguments: dict[str, Any]) -> str:
    """A stable signature for a call's arguments."""
    return "|".join(f"{name}={arguments[name]!r}" for name in sorted(arguments))


# ------------------------------------------------------------------ poster --


def poster(
    *,
    design: str,
    format: str,  # noqa: A002 - the tool's own argument name
    sheet: Sheet,
    headline: str,
    kicker: str = "",
    detail: str = "",
    photo: str = "",
    brief: StyleBrief | None = None,
    language: str = "fa",
) -> Recipe:
    """A poster built the way a poster is built.

    Two compositions, because a poster with a photograph and one without are
    different pieces of work rather than the same one with a gap in it:

    * **With a picture** it holds the upper two thirds, a wash keeps the type
      readable over whatever the picture turns out to be, and the stack runs
      kicker, headline, detail down the lower third.
    * **Without one** the type is the poster. The headline takes the middle
      band at the size the sheet will carry, a field of colour anchors the
      head so the top is not dead, and the whole thing is set to the same
      margin as the picture version.

    Every measurement is a fraction of the sheet, so the same recipe is right
    for A3 and for a six-metre hoarding.
    """
    colours = Palette.from_brief(brief)
    rtl = language in ("fa", "ar", "he", "ur")
    align = "right" if rtl else "left"
    margin = 8.0
    calls: list[Call] = [
        ("start_design", {"name": design, "format": format, "background": colours.ground}),
    ]

    if photo:
        stack_top = 62.0
        calls.append(
            (
                "place_photo",
                {
                    "design": design,
                    "name": "subject",
                    "path": photo,
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 72,
                    "units": "percent",
                    "fit": "cover",
                    "role": "subject",
                    "depth": "background",
                },
            )
        )
        # A wash over the lower half, so the type reads whatever the picture
        # underneath turns out to be. Drawn in Photoshop like every other
        # piece of furniture rather than faked with a flat rectangle.
        calls.append(
            (
                "add_furniture",
                {
                    "design": design,
                    "name": "scrim",
                    "kind": "gradient_wash",
                    "x_mm": 0,
                    "y_mm": round(sheet.height * 0.44, 2),
                    "width_mm": round(sheet.width, 2),
                    "height_mm": round(sheet.height * 0.56, 2),
                    "color": colours.ground,
                    "accent": colours.ground,
                    "ink": colours.ink,
                    "depth": "behind",
                },
            )
        )
    else:
        stack_top = 30.0
        # A field of colour across the head, so the sheet has something in it
        # above the type instead of an empty two thirds.
        calls.append(
            (
                "add_shape",
                {
                    "design": design,
                    "name": "field",
                    "shape": "rectangle",
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 18,
                    "units": "percent",
                    "color": colours.accent,
                    "role": "field",
                    "depth": "background",
                },
            )
        )
        calls.append(
            (
                "add_shape",
                {
                    "design": design,
                    "name": "counterweight",
                    "shape": "rectangle",
                    "x": 100 - margin - 22 if rtl else margin,
                    "y": 86,
                    "width": 22,
                    "height": 0.5,
                    "units": "percent",
                    "color": colours.accent,
                    "role": "rule",
                    "depth": "front",
                },
            )
        )

    cursor = stack_top
    if kicker:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "kicker",
                    "text": kicker,
                    "x": margin,
                    "y": cursor,
                    "width": 100 - margin * 2,
                    "height": 4.5,
                    "units": "percent",
                    "color": colours.accent,
                    "alignment": align,
                    "role": "kicker",
                    "tracking": 120,
                },
            )
        )
        cursor += 5.5
    calls.append(
        (
            "add_shape",
            {
                "design": design,
                "name": "accent_rule",
                "shape": "rectangle",
                "x": 100 - margin - 14 if rtl else margin,
                "y": cursor,
                "width": 14,
                "height": 0.7,
                "units": "percent",
                "color": colours.accent,
                "role": "rule",
                "depth": "front",
            },
        )
    )
    cursor += 2.5
    headline_height = 16.0 if photo else 34.0
    calls.append(
        (
            "add_text",
            {
                "design": design,
                "name": "headline",
                "text": headline,
                "x": margin,
                "y": cursor,
                "width": 100 - margin * 2,
                "height": headline_height,
                "units": "percent",
                "color": colours.ink,
                "alignment": align,
                "role": "headline",
                "tracking": -15,
            },
        )
    )
    # Enough air that the headline's descenders never reach the line below.
    cursor += headline_height + 4.0
    if detail:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "detail",
                    "text": detail,
                    "x": margin,
                    "y": cursor,
                    "width": 100 - margin * 2,
                    "height": 6.0,
                    "units": "percent",
                    "color": shift(colours.ink, lighten=-0.15),
                    "alignment": align,
                    "role": "body",
                },
            )
        )
    calls.extend(
        [
            (
                "style_layer",
                {
                    "design": design,
                    "layer": "headline",
                    "shadow": {"color": "#000000", "opacity": 38, "distance": 4, "size": 24, "angle": 120},
                },
            ),
            ("check_design", {"design": design}),
            ("render_design", {"design": design}),
            ("build_in_photoshop", {"design": design, "export_as": "png"}),
            ("save_design", {"design": design}),
        ]
    )
    return Recipe(name=f"poster '{design}'", calls=calls)


# -------------------------------------------------------------------- page --


@dataclass
class Sheet:
    """The page a recipe is laying out, in millimetres."""

    width: float
    height: float
    margin: float
    columns: int = 6
    gutter: float = 4.0
    rtl: bool = True

    @property
    def live_x(self) -> float:
        """Left edge of the live area."""
        return self.margin

    @property
    def live_y(self) -> float:
        """Top edge of the live area."""
        return self.margin

    @property
    def live_width(self) -> float:
        """Width of the live area."""
        return self.width - self.margin * 2

    @property
    def live_height(self) -> float:
        """Height of the live area."""
        return self.height - self.margin * 2

    def column_width(self) -> float:
        """Width of a single column."""
        return (self.live_width - self.gutter * (self.columns - 1)) / self.columns

    def span(self, columns: int) -> float:
        """Width of *columns* columns, gutters included."""
        return self.column_width() * columns + self.gutter * (columns - 1)

    @classmethod
    def resolve(cls, formats: Any, request: str, *, columns: int = 6, rtl: bool = True) -> Sheet:
        """Read a size out of the catalogue and give it working margins."""
        item = formats.resolve(request)
        width, height = item.width_mm, item.height_mm
        return cls(
            width=round(width, 2),
            height=round(height, 2),
            margin=round(min(width, height) * 0.05, 1),
            columns=columns,
            rtl=rtl,
        )


def news_page(
    *,
    document: str,
    format: str,  # noqa: A002 - the tool's own argument name
    sheet: Sheet,
    headline: str,
    body: str,
    kicker: str = "",
    photo: str = "",
    caption: str = "",
    sidebar: str = "",
    sidebar_heading: str = "",
    brief: StyleBrief | None = None,
) -> Recipe:
    """A news page: the furniture first, then the copy that sits on it.

    Every measurement is taken from the live area rather than written down, so
    the same recipe lays out a 381 x 476 broadsheet and an A3 tabloid without
    either of them looking stretched.
    """
    colours = Palette.from_brief(brief)
    live_w, live_h = sheet.live_width, sheet.live_height
    x, y = sheet.live_x, sheet.live_y

    aside = 2 if sidebar else 0
    main_columns = sheet.columns - aside
    main_w = sheet.span(main_columns)
    aside_w = sheet.span(aside) if aside else 0.0
    # The sidebar sits at the end of the reading direction: the left of a
    # Persian page, the right of an English one.
    main_x = x + (live_w - main_w) if sheet.rtl else x
    aside_x = x if sheet.rtl else x + live_w - aside_w

    calls: list[Call] = [
        (
            "start_document",
            {
                "name": document,
                "format": format,
                "pages": 1,
                "columns": sheet.columns,
                "product_type": "newspaper",
            },
        )
    ]

    cursor = y
    if kicker:
        height = max(5.0, live_h * 0.022)
        calls.append(
            (
                "add_text_frame",
                {
                    "document": document,
                    "page": 1,
                    "name": "kicker",
                    "text": kicker,
                    "x_mm": round(main_x, 2),
                    "y_mm": round(cursor, 2),
                    "width_mm": round(main_w, 2),
                    "height_mm": round(height, 2),
                    "style": "kicker",
                },
            )
        )
        cursor += height + live_h * 0.008

    headline_height = max(12.0, live_h * 0.085)
    calls.append(
        (
            "add_text_frame",
            {
                "document": document,
                "page": 1,
                "name": "headline",
                "text": headline,
                "x_mm": round(main_x, 2),
                "y_mm": round(cursor, 2),
                "width_mm": round(main_w, 2),
                "height_mm": round(headline_height, 2),
                "style": "headline",
            },
        )
    )
    cursor += headline_height + live_h * 0.012

    if photo:
        picture_height = live_h * 0.28
        calls.append(
            (
                "add_picture_frame",
                {
                    "document": document,
                    "page": 1,
                    "name": "lead_photo",
                    "path": photo,
                    "x_mm": round(main_x, 2),
                    "y_mm": round(cursor, 2),
                    "width_mm": round(main_w, 2),
                    "height_mm": round(picture_height, 2),
                    "fit": "fill",
                    "caption": caption,
                },
            )
        )
        # The caption takes its own frame under the picture; leave room for it.
        cursor += picture_height + (live_h * 0.035 if caption else live_h * 0.012)

    body_height = max(20.0, y + live_h - cursor)
    calls.append(
        (
            "add_text_frame",
            {
                "document": document,
                "page": 1,
                "name": "body",
                "text": body,
                "x_mm": round(main_x, 2),
                "y_mm": round(cursor, 2),
                "width_mm": round(main_w, 2),
                "height_mm": round(body_height, 2),
                "style": "body",
                "columns": max(1, main_columns // 2),
            },
        )
    )

    if sidebar:
        panel_y = y + live_h * 0.14
        panel_height = live_h * 0.52
        calls.append(
            (
                "add_page_furniture",
                {
                    "document": document,
                    "page": 1,
                    "name": "sidebar_panel",
                    "kind": "tint_panel",
                    "x_mm": round(aside_x, 2),
                    "y_mm": round(panel_y, 2),
                    "width_mm": round(aside_w, 2),
                    "height_mm": round(panel_height, 2),
                    "color": colours.paper,
                    "accent": colours.accent,
                    "ink": "#111111",
                    "corner_mm": 2.0,
                },
            )
        )
        inset = min(6.0, aside_w * 0.08)
        heading_height = max(7.0, panel_height * 0.09)
        if sidebar_heading:
            calls.append(
                (
                    "add_text_frame",
                    {
                        "document": document,
                        "page": 1,
                        "name": "sidebar_heading",
                        "text": sidebar_heading,
                        "x_mm": round(aside_x + inset, 2),
                        "y_mm": round(panel_y + inset, 2),
                        "width_mm": round(aside_w - inset * 2, 2),
                        "height_mm": round(heading_height, 2),
                        "style": "subheadline",
                    },
                )
            )
        text_y = panel_y + inset + (heading_height + 2.0 if sidebar_heading else 0.0)
        calls.append(
            (
                "add_text_frame",
                {
                    "document": document,
                    "page": 1,
                    "name": "sidebar_text",
                    "text": sidebar,
                    "x_mm": round(aside_x + inset, 2),
                    "y_mm": round(text_y, 2),
                    "width_mm": round(aside_w - inset * 2, 2),
                    "height_mm": round(panel_y + panel_height - inset - text_y, 2),
                    "style": "sidebar",
                },
            )
        )

    calls.extend(
        [
            ("inspect_document", {"document": document}),
            ("preview_page", {"document": document, "page": 1}),
            ("build_in_indesign", {"document": document}),
            ("export_document_pdf", {"document": document, "preset": "print"}),
        ]
    )
    return Recipe(name=f"news page '{document}'", calls=calls)


# ------------------------------------------------------------------- video --


def social_cut(
    *,
    edit: str,
    format: str,  # noqa: A002 - the tool's own argument name
    footage: list[str],
    overlay: str = "",
    music: str = "",
    shot_seconds: float = 3.0,
    transition: str = "Cross Dissolve",
) -> Recipe:
    """A short cut: the shots, a dissolve between them, the title over the top.

    The overlay is whatever the Photoshop specialist published, which is what
    makes a design on a video a design rather than a Premiere title.
    """
    calls: list[Call] = [
        ("start_edit", {"name": edit, "format": format}),
    ]
    if footage:
        calls.append(("import_footage", {"edit": edit, "paths": list(footage)}))
    for index, item in enumerate(footage):
        calls.append(
            (
                "add_clip",
                {
                    "edit": edit,
                    "item": item,
                    "track": 0,
                    "in_point": 0.0,
                    "out_point": round(shot_seconds, 2),
                    "note": f"shot {index + 1}",
                },
            )
        )
    for index in range(max(0, len(footage) - 1)):
        calls.append(
            (
                "add_transition",
                {
                    "edit": edit,
                    "transition": transition,
                    "after_clip": index,
                    "duration": 0.6,
                    "track": 0,
                },
            )
        )
    if overlay:
        calls.append(
            (
                "add_overlay",
                {
                    "edit": edit,
                    "path": overlay,
                    "at": 0.0,
                    "duration": round(max(2.0, shot_seconds * len(footage) * 0.45), 2),
                    "fade": 0.4,
                    "track": 1,
                },
            )
        )
    if music:
        calls.append(
            (
                "add_audio",
                {
                    "edit": edit,
                    "item": music,
                    "track": 0,
                    "at": 0.0,
                    "duration": round(max(2.0, shot_seconds * max(1, len(footage))), 2),
                },
            )
        )
    calls.extend(
        [
            ("inspect_edit", {"edit": edit}),
            ("build_in_premiere", {"edit": edit}),
            ("save_edit", {"edit": edit}),
        ]
    )
    return Recipe(name=f"cut '{edit}'", calls=calls)


# --------------------------------------------------------------- artwork ----


def feature_image(
    *,
    design: str,
    width_mm: float,
    height_mm: float,
    dpi: int = 300,
    photo: str = "",
    caption: str = "",
    credit: str = "",
    brief: StyleBrief | None = None,
    language: str = "fa",
) -> Recipe:
    """The picture that goes on a page, built rather than merely cropped.

    A photograph graded into the paper's palette, a wash so a caption reads
    over its foot, and the caption bar itself - the piece InDesign then places
    as a single image. No headline: the headline belongs on the page, and
    setting it twice is the mark of a machine rather than a designer.
    """
    colours = Palette.from_brief(brief)
    rtl = language in ("fa", "ar", "he", "ur")
    size = f"{width_mm:g}x{height_mm:g} mm"
    calls: list[Call] = [
        ("start_design", {"name": design, "format": size, "background": colours.ground, "dpi": dpi}),
    ]
    if photo:
        calls.append(
            (
                "place_photo",
                {
                    "design": design,
                    "name": "photograph",
                    "path": photo,
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 100,
                    "units": "percent",
                    "fit": "cover",
                    "role": "subject",
                    "depth": "background",
                },
            )
        )
    if caption or credit:
        bar_height = max(8.0, height_mm * 0.16)
        calls.append(
            (
                "add_furniture",
                {
                    "design": design,
                    "name": "caption_bar",
                    "kind": "caption_bar",
                    "x_mm": 0,
                    "y_mm": round(height_mm - bar_height, 2),
                    "width_mm": round(width_mm, 2),
                    "height_mm": round(bar_height, 2),
                    "color": colours.ground,
                    "accent": colours.accent,
                    "ink": colours.ink,
                    "depth": "front",
                },
            )
        )
        text = caption or credit
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "caption",
                    "text": text if not credit or not caption else f"{caption} - {credit}",
                    "x": 4,
                    "y": round(100 - (bar_height / height_mm * 100) + 3, 2),
                    "width": 92,
                    "height": round(bar_height / height_mm * 100 - 6, 2),
                    "units": "percent",
                    "color": colours.ink,
                    "alignment": "right" if rtl else "left",
                    "role": "caption",
                    "depth": "top",
                },
            )
        )
    calls.extend(
        [
            ("check_design", {"design": design}),
            ("build_in_photoshop", {"design": design, "export_as": "png"}),
            ("save_design", {"design": design}),
        ]
    )
    return Recipe(name=f"feature image '{design}'", calls=calls)


def title_card(
    *,
    design: str,
    format: str,  # noqa: A002 - the tool's own argument name
    headline: str,
    kicker: str = "",
    detail: str = "",
    brief: StyleBrief | None = None,
    language: str = "fa",
) -> Recipe:
    """A title over footage: type on transparency, with a bar to hold it.

    Built at the frame size on a transparent ground so Premiere lays it over
    the picture rather than covering it, and set inside the safe area so a
    phone's own interface does not sit on the words.
    """
    colours = Palette.from_brief(brief)
    rtl = language in ("fa", "ar", "he", "ur")
    align = "right" if rtl else "left"
    calls: list[Call] = [
        ("start_design", {"name": design, "format": format, "background": "#00000000", "mode": "rgb"}),
        (
            "add_shape",
            {
                "design": design,
                "name": "bar",
                "shape": "rectangle",
                "x": 6,
                "y": 62,
                "width": 88,
                "height": 24,
                "units": "percent",
                "color": colours.ground,
                "opacity": 78,
                "role": "scrim",
                "depth": "behind",
            },
        ),
    ]
    if kicker:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "kicker",
                    "text": kicker,
                    "x": 10,
                    "y": 64.5,
                    "width": 80,
                    "height": 4,
                    "units": "percent",
                    "color": colours.accent,
                    "alignment": align,
                    "tracking": 140,
                    "role": "kicker",
                },
            )
        )
    calls.append(
        (
            "add_text",
            {
                "design": design,
                "name": "headline",
                "text": headline,
                "x": 10,
                "y": 69,
                "width": 80,
                "height": 11,
                "units": "percent",
                "color": colours.ink,
                "alignment": align,
                "role": "headline",
                "tracking": -10,
            },
        )
    )
    if detail:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "detail",
                    "text": detail,
                    "x": 10,
                    "y": 80.5,
                    "width": 80,
                    "height": 4.5,
                    "units": "percent",
                    "color": colours.ink,
                    "alignment": align,
                    "role": "body",
                },
            )
        )
    calls.extend(
        [
            ("check_design", {"design": design}),
            # PNG, and unflattened: a title card that lost its transparency is
            # a rectangle over the footage rather than type on it.
            ("build_in_photoshop", {"design": design, "export_as": "png", "flatten": False}),
            ("save_design", {"design": design}),
        ]
    )
    return Recipe(name=f"title card '{design}'", calls=calls)


# ----------------------------------------------------------- compositing ---


def composite_cover(
    *,
    design: str,
    format: str,  # noqa: A002 - the tool's own argument name
    sheet: Sheet,
    headline: str,
    kicker: str = "",
    detail: str = "",
    photo: str = "",
    subject: str = "",
    brief: StyleBrief | None = None,
    language: str = "fa",
    texture: str = Texture.PAPER.value,
) -> Recipe:
    """The cover a picture desk builds, rather than a photograph with type on it.

    The photograph fills the sheet; the subject is cut out of it and set back
    down over a band, with a halo in the accent colour so it lifts off; the
    headline is reversed *out* of the band, so the picture shows through the
    letters; and a surface over the whole thing stops it looking like flat
    colour. Each of those is a separate decision a designer makes, and each is
    a separate tool call here.
    """
    colours = Palette.from_brief(brief)
    rtl = language in ("fa", "ar", "he", "ur")
    align = "right" if rtl else "left"
    margin = 8.0
    calls: list[Call] = [
        ("start_design", {"name": design, "format": format, "background": colours.ground}),
    ]

    if photo:
        calls.append(
            (
                "place_photo",
                {
                    "design": design,
                    "name": "photograph",
                    "path": photo,
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 100,
                    "units": "percent",
                    "fit": "cover",
                    "role": "subject",
                    "depth": "background",
                },
            )
        )
    if subject:
        # Cut the subject off its background before it is placed, so the halo
        # follows the shoulders rather than the edge of a rectangle.
        calls.extend(
            [
                (
                    "prepare_photo",
                    {"path": subject, "name": f"{design}_subject", "cut_out": True},
                ),
                (
                    "place_photo",
                    {
                        "design": design,
                        "name": "cutout",
                        "path": f"{design}_subject",
                        "x": 8,
                        "y": 14,
                        "width": 84,
                        "height": 62,
                        "units": "percent",
                        "fit": "contain",
                        "role": "subject",
                        "depth": "front",
                    },
                ),
                (
                    "style_layer",
                    {
                        "design": design,
                        "layer": "cutout",
                        "stroke": {"color": colours.accent, "size": 22, "opacity": 100},
                        "shadow": {
                            "color": "#000000",
                            "opacity": 45,
                            "distance": 10,
                            "size": 40,
                            "angle": 120,
                        },
                    },
                ),
            ]
        )

    calls.append(
        (
            "add_shape",
            {
                "design": design,
                "name": "band",
                "shape": "rectangle",
                "x": 0,
                "y": 62,
                "width": 100,
                "height": 26,
                "units": "percent",
                "color": colours.ground,
                "role": "scrim",
                "depth": "front",
            },
        )
    )
    if kicker:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "kicker",
                    "text": kicker,
                    "x": margin,
                    "y": 64,
                    "width": 100 - margin * 2,
                    "height": 4.0,
                    "units": "percent",
                    "color": colours.accent,
                    "alignment": align,
                    "tracking": 140,
                    "role": "kicker",
                    "depth": "top",
                },
            )
        )
    calls.extend(
        [
            (
                "add_text",
                {
                    "design": design,
                    "name": "headline",
                    "text": headline,
                    "x": margin,
                    "y": 69,
                    "width": 100 - margin * 2,
                    "height": 13,
                    "units": "percent",
                    "color": colours.ink,
                    "alignment": align,
                    "tracking": -15,
                    "role": "headline",
                    "depth": "top",
                },
            ),
            # The letters become holes in the band, so the photograph runs
            # through them. This is the move the whole composite is built for.
            ("reverse_out", {"design": design, "layer": "headline", "out_of": "band"}),
        ]
    )
    if detail:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "detail",
                    "text": detail,
                    "x": margin,
                    "y": 83,
                    "width": 100 - margin * 2,
                    "height": 4.5,
                    "units": "percent",
                    "color": shift(colours.ink, lighten=-0.1),
                    "alignment": align,
                    "role": "body",
                    "depth": "top",
                },
            )
        )
    calls.extend(
        [
            ("add_texture", {"design": design, "name": "stock", "kind": texture}),
            ("check_design", {"design": design}),
            ("render_design", {"design": design}),
            ("build_in_photoshop", {"design": design, "export_as": "png"}),
            ("save_design", {"design": design}),
        ]
    )
    return Recipe(name=f"composite cover '{design}'", calls=calls)


# ------------------------------------------------------------- editorial ---


def editorial_poster(
    *,
    design: str,
    format: str,  # noqa: A002 - the tool's own argument name
    sheet: Sheet,
    headline: str,
    kicker: str = "",
    detail: str = "",
    photo: str = "",
    brief: StyleBrief | None = None,
    language: str = "fa",
) -> Recipe:
    """The quiet one: a strict grid, a great deal of white, hairlines.

    Everything sits on the same column edge and nothing is centred. The
    picture is small and deliberately placed rather than filling the sheet,
    the type is set at a reading size instead of a shouting one, and the
    hierarchy is carried by space and by two hairline rules. It is the
    opposite of the photographic and typographic approaches on purpose - a
    choice of three that are all loud is not a choice.
    """
    colours = Palette.from_brief(brief)
    rtl = language in ("fa", "ar", "he", "ur")
    align = "right" if rtl else "left"
    margin = 12.0
    # The measure sits on a third of the sheet, which is what makes the white
    # space read as a decision rather than as a gap.
    measure = 46.0
    left = 100 - margin - measure if rtl else margin

    calls: list[Call] = [
        ("start_design", {"name": design, "format": format, "background": colours.paper}),
        (
            "add_shape",
            {
                "design": design,
                "name": "rule_top",
                "shape": "rectangle",
                "x": margin,
                "y": 16,
                "width": 100 - margin * 2,
                "height": 0.18,
                "units": "percent",
                "color": colours.ink,
                "role": "rule",
            },
        ),
    ]
    if kicker:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "kicker",
                    "text": kicker,
                    "x": left,
                    "y": 18,
                    "width": measure,
                    "height": 3.2,
                    "units": "percent",
                    "color": colours.accent,
                    "alignment": align,
                    "tracking": 180,
                    "role": "kicker",
                },
            )
        )
    calls.append(
        (
            "add_text",
            {
                "design": design,
                "name": "headline",
                "text": headline,
                "x": left,
                "y": 23,
                "width": measure,
                "height": 22,
                "units": "percent",
                "color": readable_on(colours.paper),
                "alignment": align,
                "tracking": -8,
                "role": "headline",
            },
        )
    )
    if detail:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "detail",
                    "text": detail,
                    "x": left,
                    "y": 47,
                    "width": measure,
                    "height": 9,
                    "units": "percent",
                    "color": shift(readable_on(colours.paper), lighten=0.25),
                    "alignment": align,
                    "role": "body",
                },
            )
        )
    if photo:
        # Small, low, and on the opposite column edge from the type: the
        # picture answers the text rather than competing with it.
        picture_left = margin if rtl else 100 - margin - 34
        calls.append(
            (
                "place_photo",
                {
                    "design": design,
                    "name": "picture",
                    "path": photo,
                    "x": picture_left,
                    "y": 58,
                    "width": 34,
                    "height": 26,
                    "units": "percent",
                    "fit": "cover",
                    "role": "subject",
                },
            )
        )
    calls.extend(
        [
            (
                "add_shape",
                {
                    "design": design,
                    "name": "rule_foot",
                    "shape": "rectangle",
                    "x": margin,
                    "y": 90,
                    "width": 100 - margin * 2,
                    "height": 0.18,
                    "units": "percent",
                    "color": colours.ink,
                    "role": "rule",
                },
            ),
            ("check_design", {"design": design}),
            ("render_design", {"design": design}),
            ("build_in_photoshop", {"design": design, "export_as": "png"}),
            ("save_design", {"design": design}),
        ]
    )
    return Recipe(name=f"editorial '{design}'", calls=calls)


# --------------------------------------------------------------- graphic ---


def graphic_poster(
    *,
    design: str,
    format: str,  # noqa: A002 - the tool's own argument name
    sheet: Sheet,
    headline: str,
    kicker: str = "",
    detail: str = "",
    photo: str = "",
    brief: StyleBrief | None = None,
    language: str = "fa",
) -> Recipe:
    """The loud one: shape and colour carry it, and the type sits inside them.

    A field of accent cut by a diagonal, a disc the type overlaps, and a
    photograph - if there is one - masked into the disc rather than laid
    across the sheet. Nothing here is a photograph with words on it, which is
    the point: it is the concept that survives having no picture at all.
    """
    colours = Palette.from_brief(brief)
    rtl = language in ("fa", "ar", "he", "ur")
    align = "right" if rtl else "left"
    margin = 9.0

    calls: list[Call] = [
        ("start_design", {"name": design, "format": format, "background": colours.ground}),
        (
            "add_shape",
            {
                "design": design,
                "name": "field",
                "shape": "polygon",
                "x": 0,
                "y": 0,
                "width": 100,
                "height": 62,
                "units": "percent",
                "color": colours.accent,
                "points": [[0, 0], [100, 0], [100, 74], [0, 100]],
                "role": "field",
                "depth": "background",
            },
        ),
        (
            "add_shape",
            {
                "design": design,
                "name": "disc",
                "shape": "ellipse",
                "x": 56 if rtl else 8,
                "y": 26,
                "width": 36,
                "height": 26,
                "units": "percent",
                "color": colours.ground,
                "role": "field",
                "depth": "behind",
            },
        ),
    ]
    if photo:
        # Into the disc, not across the sheet.
        calls.append(
            (
                "place_photo",
                {
                    "design": design,
                    "name": "picture",
                    "path": photo,
                    "x": 56 if rtl else 8,
                    "y": 26,
                    "width": 36,
                    "height": 26,
                    "units": "percent",
                    "fit": "cover",
                    "clip_to_below": True,
                    "role": "subject",
                },
            )
        )
    if kicker:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "kicker",
                    "text": kicker,
                    "x": margin,
                    "y": 8,
                    "width": 100 - margin * 2,
                    "height": 4,
                    "units": "percent",
                    "color": readable_on(colours.accent),
                    "alignment": align,
                    "tracking": 200,
                    "role": "kicker",
                    "depth": "top",
                },
            )
        )
    calls.append(
        (
            "add_text",
            {
                "design": design,
                "name": "headline",
                "text": headline,
                "x": margin,
                "y": 58,
                "width": 100 - margin * 2,
                "height": 24,
                "units": "percent",
                "color": colours.ink,
                "alignment": align,
                "tracking": -22,
                "role": "headline",
                "depth": "top",
            },
        )
    )
    if detail:
        calls.append(
            (
                "add_text",
                {
                    "design": design,
                    "name": "detail",
                    "text": detail,
                    "x": margin,
                    "y": 84,
                    "width": 100 - margin * 2,
                    "height": 6,
                    "units": "percent",
                    "color": shift(colours.ink, lighten=-0.12),
                    "alignment": align,
                    "role": "body",
                    "depth": "top",
                },
            )
        )
    calls.extend(
        [
            ("check_design", {"design": design}),
            ("render_design", {"design": design}),
            ("build_in_photoshop", {"design": design, "export_as": "png"}),
            ("save_design", {"design": design}),
        ]
    )
    return Recipe(name=f"graphic '{design}'", calls=calls)
