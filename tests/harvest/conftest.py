"""A publication built to known measurements, so a measurement can be checked.

Harvesting a real newspaper is the point of the module, but a real newspaper
cannot say what its own margins are. These fixtures draw pages whose grid,
margins, boxes and rules are known exactly, so the reading can be compared
with the truth rather than with an impression.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tests.studio.conftest import (  # noqa: F401 - re-exported as fixtures
    offline_adobe,
    photograph,
    studio_context,
)

DPI = 110
PAPER = (252, 251, 248)
INK = (24, 24, 28)
TINT = (226, 224, 218)
ACCENT = (194, 65, 12)


@dataclass
class Sheet:
    """The truth about a page this fixture drew."""

    width_mm: float = 300.0
    height_mm: float = 430.0
    margin_mm: float = 18.0
    columns: int = 6
    gutter_mm: float = 5.0
    body_pt: float = 9.0

    @property
    def column_width_mm(self) -> float:
        live = self.width_mm - self.margin_mm * 2
        return (live - self.gutter_mm * (self.columns - 1)) / self.columns

    def px(self, millimetres: float) -> int:
        return int(round(millimetres / 25.4 * DPI))


def draw_page(
    sheet: Sheet,
    *,
    seed: int = 0,
    panels: list[tuple[float, float, float, float]] | None = None,
    keyline_box: tuple[float, float, float, float] | None = None,
    spanning_picture: bool = False,
    rules: bool = True,
) -> Image.Image:
    """A page of ruled columns of body copy, with boxes on it.

    The "type" is drawn as bars of the right depth and leading rather than as
    real glyphs: the harvester measures bands of ink, so bars exercise exactly
    what it looks at without dragging a font into the test.
    """
    random.seed(seed)
    image = Image.new("RGB", (sheet.px(sheet.width_mm), sheet.px(sheet.height_mm)), PAPER)
    draw = ImageDraw.Draw(image)

    live_top = sheet.margin_mm
    live_bottom = sheet.height_mm - sheet.margin_mm
    line_mm = sheet.body_pt / 72 * 25.4
    leading_mm = line_mm * 1.45
    # A line of type inks its ascender to its descender, which is about
    # seven tenths of its point size; a bar of that depth is what the
    # harvester actually looks at.
    ink_mm = line_mm * 0.72

    picture_bottom = live_top
    if spanning_picture:
        picture_bottom = live_top + (live_bottom - live_top) * 0.34
        draw.rectangle(
            [
                sheet.px(sheet.margin_mm),
                sheet.px(live_top),
                sheet.px(sheet.width_mm - sheet.margin_mm),
                sheet.px(picture_bottom),
            ],
            fill=(120, 130, 150),
        )
        picture_bottom += 4.0

    for index in range(sheet.columns):
        x = sheet.margin_mm + index * (sheet.column_width_mm + sheet.gutter_mm)
        y = picture_bottom
        while y + line_mm < live_bottom:
            # A ragged right edge, the way set copy actually looks.
            width = sheet.column_width_mm * random.uniform(0.72, 1.0)
            draw.rectangle(
                [sheet.px(x), sheet.px(y), sheet.px(x + width), sheet.px(y + ink_mm)],
                fill=INK,
            )
            y += leading_mm

    for box in panels or []:
        x, y, width, height = box
        draw.rectangle(
            [sheet.px(x), sheet.px(y), sheet.px(x + width), sheet.px(y + height)], fill=TINT
        )

    if keyline_box:
        x, y, width, height = keyline_box
        draw.rectangle(
            [sheet.px(x), sheet.px(y), sheet.px(x + width), sheet.px(y + height)],
            fill=PAPER,
            outline=ACCENT,
            width=max(2, sheet.px(0.5)),
        )

    if rules:
        for y in (live_top - 3.0, live_bottom + 2.0):
            draw.rectangle(
                [
                    sheet.px(sheet.margin_mm),
                    sheet.px(y),
                    sheet.px(sheet.width_mm - sheet.margin_mm),
                    sheet.px(y + 0.5),
                ],
                fill=INK,
            )
    return image


@pytest.fixture(scope="session")
def sheet_spec() -> Sheet:
    """The measurements every page in these tests is drawn to."""
    return Sheet()


@pytest.fixture
def sheet(sheet_spec: Sheet) -> Sheet:
    """The same measurements, for a test that wants them by the shorter name."""
    return sheet_spec


@pytest.fixture
def page_image(tmp_path: Path, sheet: Sheet) -> Path:
    """One page, written out."""
    target = tmp_path / "page01.png"
    draw_page(sheet, panels=[(230.0, 90.0, 52.0, 120.0)]).save(target)
    return target


@pytest.fixture(scope="session")
def publication(tmp_path_factory, sheet_spec: Sheet) -> list[Path]:
    """Six pages of one paper, with the same box on several of them.

    Drawn once for the whole run: rendering and measuring six broadsheet pages
    is the entire cost of this suite, and every test wants the same six.
    """
    sheet = sheet_spec
    tmp_path = tmp_path_factory.mktemp("publication")
    out: list[Path] = []
    for index in range(1, 7):
        panels = [(230.0, 90.0, 52.0, 120.0)] if index % 2 else [(18.0, 300.0, 52.0, 120.0)]
        image = draw_page(
            sheet,
            seed=index,
            panels=panels,
            keyline_box=(90.0, 260.0, 80.0, 60.0) if index == 3 else None,
            spanning_picture=index in (1, 4),
        )
        target = tmp_path / f"page{index:02d}.png"
        image.save(target)
        out.append(target)
    return out
