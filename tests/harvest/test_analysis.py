"""Measuring one page."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.harvest.analysis import measure_page, paper_colour, type_scale
from app.harvest.reader import read_images
from tests.harvest.conftest import Sheet, draw_page


def _measure(image_path: Path, sheet: Sheet):
    page = read_images([image_path], width_mm=sheet.width_mm)[0]
    try:
        return measure_page(page)
    finally:
        page.close()


def test_the_paper_colour_is_the_stock_not_the_ink(page_image: Path) -> None:
    with Image.open(page_image) as image:
        found = paper_colour(image.convert("RGB"))

    assert all(band > 200 for band in found), found


def test_the_margins_are_where_the_type_stops(page_image: Path, sheet: Sheet) -> None:
    measured = _measure(page_image, sheet)

    assert measured.margin_left_mm == pytest.approx(sheet.margin_mm, abs=3.0)
    assert measured.margin_right_mm == pytest.approx(sheet.margin_mm, abs=3.0)
    assert measured.margin_top_mm == pytest.approx(sheet.margin_mm, abs=5.0)


def test_the_column_grid_is_recovered(page_image: Path, sheet: Sheet) -> None:
    measured = _measure(page_image, sheet)

    assert measured.columns == sheet.columns
    assert measured.column_width_mm == pytest.approx(sheet.column_width_mm, abs=2.5)
    assert measured.gutter_mm == pytest.approx(sheet.gutter_mm, abs=1.5)


def test_a_picture_across_the_page_does_not_hide_the_grid(tmp_path: Path, sheet: Sheet) -> None:
    """This is why the grid is fitted rather than counted."""
    target = tmp_path / "spanned.png"
    draw_page(sheet, spanning_picture=True).save(target)

    measured = _measure(target, sheet)

    assert measured.columns == sheet.columns


@pytest.mark.parametrize("columns", [3, 4, 5, 6, 8])
def test_grids_of_several_widths_are_all_read(tmp_path: Path, columns: int) -> None:
    sheet = Sheet(columns=columns)
    target = tmp_path / f"grid{columns}.png"
    draw_page(sheet).save(target)

    measured = _measure(target, sheet)

    assert measured.columns == columns


def test_a_page_set_to_one_measure_is_not_given_a_grid(tmp_path: Path) -> None:
    sheet = Sheet(columns=1, gutter_mm=0.0)
    target = tmp_path / "single.png"
    draw_page(sheet).save(target)

    measured = _measure(target, sheet)

    assert measured.columns == 1
    assert measured.gutter_mm == 0.0


def test_a_tint_panel_is_found_where_it_was_drawn(tmp_path: Path, sheet: Sheet) -> None:
    box = (230.0, 90.0, 52.0, 120.0)
    target = tmp_path / "panel.png"
    draw_page(sheet, panels=[box]).save(target)

    measured = _measure(target, sheet)

    found = [
        panel
        for panel in measured.panels
        if abs(panel.x_mm - box[0]) < 8 and abs(panel.y_mm - box[1]) < 8
    ]
    assert found, [panel.to_dict() for panel in measured.panels]
    panel = found[0]
    assert panel.width_mm == pytest.approx(box[2], abs=8.0)
    assert panel.height_mm == pytest.approx(box[3], abs=8.0)
    assert not panel.keyline, "a filled panel is not a ruled box"


def test_a_ruled_box_is_told_apart_from_a_filled_one(tmp_path: Path, sheet: Sheet) -> None:
    """They are drawn differently, so confusing them makes a page look wrong."""
    target = tmp_path / "keyline.png"
    draw_page(sheet, keyline_box=(90.0, 260.0, 80.0, 60.0)).save(target)

    measured = _measure(target, sheet)

    ruled = [panel for panel in measured.panels if panel.keyline]
    assert ruled, [panel.to_dict() for panel in measured.panels]
    assert ruled[0].keyline_pt > 0


def test_lines_of_type_are_not_reported_as_rules(page_image: Path, sheet: Sheet) -> None:
    """A page of Persian body copy has hundreds of long unbroken runs of ink."""
    measured = _measure(page_image, sheet)

    assert len(measured.rules) <= 12, [rule.to_dict() for rule in measured.rules[:6]]


def test_the_rules_that_are_there_are_found(page_image: Path, sheet: Sheet) -> None:
    measured = _measure(page_image, sheet)

    horizontal = [rule for rule in measured.rules if rule.orientation == "horizontal"]
    assert horizontal, "the rules above and below the live area"
    assert max(rule.length_mm for rule in horizontal) > sheet.width_mm * 0.7


def test_the_type_size_is_measured_to_within_a_point(page_image: Path, sheet: Sheet) -> None:
    measured = _measure(page_image, sheet)

    sizes = type_scale(measured.bands)
    assert sizes, "no type was measured at all"
    closest = min(sizes, key=lambda size: abs(size - sheet.body_pt))
    assert closest == pytest.approx(sheet.body_pt, abs=1.5), sizes


def test_a_photograph_is_not_reported_as_enormous_type(tmp_path: Path, sheet: Sheet) -> None:
    target = tmp_path / "picture.png"
    draw_page(sheet, spanning_picture=True).save(target)

    measured = _measure(target, sheet)

    assert all(band.size_pt < 100 for band in measured.bands), [
        band.to_dict() for band in measured.bands if band.size_pt >= 100
    ]


def test_the_accent_of_a_ruled_box_reaches_the_palette(tmp_path: Path, sheet: Sheet) -> None:
    target = tmp_path / "accent.png"
    draw_page(sheet, keyline_box=(60.0, 200.0, 160.0, 120.0)).save(target)

    measured = _measure(target, sheet)

    assert measured.palette
