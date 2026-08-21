"""Reducing a run of pages to one design system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.harvest.harvester import PublicationHarvester
from app.harvest.publication import DesignSystem, redraw_furniture, scaled_furniture
from tests.harvest.conftest import Sheet, draw_page


@pytest.fixture(scope="session")
def system(publication: list[Path], sheet_spec: Sheet) -> DesignSystem:
    """The system harvested from six pages of one paper, measured once."""
    return PublicationHarvester(max_pages=8).harvest_images(
        publication, width_mm=sheet_spec.width_mm, name="Test Paper"
    )


def test_the_sheet_is_read_exactly(system: DesignSystem, sheet: Sheet) -> None:
    assert system.page_width_mm == pytest.approx(sheet.width_mm, abs=0.5)
    assert system.page_height_mm == pytest.approx(sheet.height_mm, abs=1.0)
    assert system.confidence["sheet"] == 1.0


def test_the_margins_are_the_ones_the_paper_is_set_to(
    system: DesignSystem, sheet: Sheet
) -> None:
    for measured in (
        system.margin_top_mm,
        system.margin_inside_mm,
        system.margin_outside_mm,
    ):
        assert measured == pytest.approx(sheet.margin_mm, abs=3.0)

    # The foot is different, and honestly so: a column bottoms out on its own
    # baseline grid, so the last line stops somewhere in the leading above the
    # margin. Measuring off pixels cannot see the margin the column was set
    # to, only where its type stopped, so the foot reads up to one line deep.
    leading_mm = sheet.body_pt / 72 * 25.4 * 1.45
    assert sheet.margin_mm - 1.0 <= system.margin_bottom_mm <= sheet.margin_mm + leading_mm + 1.0


def test_a_page_bleeding_a_picture_does_not_set_the_paper_margin_to_nothing(
    tmp_path: Path, sheet: Sheet
) -> None:
    """A margin is a limit; one page reaching the trim is not the paper's rule."""
    from PIL import ImageDraw

    pages = []
    for index in range(1, 7):
        image = draw_page(sheet, seed=index)
        if index == 2:
            ImageDraw.Draw(image).rectangle(
                [0, 0, image.width, sheet.px(60.0)], fill=(90, 100, 120)
            )
        target = tmp_path / f"p{index}.png"
        image.save(target)
        pages.append(target)

    system = PublicationHarvester().harvest_images(pages, width_mm=sheet.width_mm)

    assert system.margin_top_mm > 5.0


def test_the_grid_is_the_one_most_pages_are_on(system: DesignSystem, sheet: Sheet) -> None:
    assert system.columns == sheet.columns
    assert system.column_width_mm == pytest.approx(sheet.column_width_mm, abs=3.0)


def test_a_paper_set_to_one_measure_is_reported_as_one(tmp_path: Path) -> None:
    """A magazine is not a six-column paper because one page happened to fit."""
    sheet = Sheet(columns=1, gutter_mm=0.0)
    pages = []
    for index in range(1, 6):
        target = tmp_path / f"p{index}.png"
        draw_page(sheet, seed=index).save(target)
        pages.append(target)

    system = PublicationHarvester().harvest_images(pages, width_mm=sheet.width_mm)

    assert system.columns == 1
    assert system.gutter_mm == 0.0
    assert any("single measure" in note for note in system.notes)


def test_the_type_scale_is_recovered(system: DesignSystem, sheet: Sheet) -> None:
    assert system.sizes_pt
    closest = min(system.sizes_pt, key=lambda size: abs(size - sheet.body_pt))
    assert closest == pytest.approx(sheet.body_pt, abs=1.5), system.sizes_pt


def test_the_type_scale_is_given_the_names_a_template_uses(system: DesignSystem) -> None:
    roles = system.role_sizes()

    assert "body" in roles
    if "headline" in roles:
        assert roles["headline"] >= roles["body"]


def test_the_boxes_are_grouped_across_pages(system: DesignSystem) -> None:
    """The same sidebar box on six pages is one piece of furniture, not six."""
    assert system.furniture
    kinds = [piece.kind.value for piece in system.furniture]
    assert "tint_panel" in kinds
    panels = [piece for piece in system.furniture if piece.kind.value == "tint_panel"]
    assert len(panels) <= 4, kinds


def test_a_harvested_box_can_be_redrawn_at_a_new_size(system: DesignSystem, tmp_path: Path) -> None:
    """This is the point of measuring them: copying the box, not a picture of it."""
    from app.design.furniture import FurnitureFactory

    kind = system.furniture[0].kind.value
    resized = scaled_furniture(system, kind, width_mm=120.0, height_mm=45.0)

    assert resized.width_mm == 120.0
    assert resized.height_mm == 45.0
    assert resized.color == system.furniture[0].color, "the paper's own colour survives"

    factory = FurnitureFactory(tmp_path / "furniture")
    drawn = factory.make(resized)
    assert drawn.exists()


def test_asking_for_a_box_the_paper_does_not_have_says_which_it_does(
    system: DesignSystem,
) -> None:
    with pytest.raises(ValueError, match="has no"):
        scaled_furniture(system, "quote_plate", width_mm=60.0, height_mm=40.0)


def test_every_box_is_redrawn(system: DesignSystem, tmp_path: Path) -> None:
    from app.design.furniture import FurnitureFactory

    drawn = redraw_furniture(system, FurnitureFactory(tmp_path / "f"), limit=4)

    assert drawn
    assert all(item["drawn"] for item in drawn), drawn
    for item in drawn:
        assert Path(item["path"]).exists()


def test_the_system_becomes_a_template_the_studio_can_lay_out_on(
    system: DesignSystem, template, sheet: Sheet
) -> None:
    spec = system.to_template(language="fa", base=template)

    assert spec.page_width_mm == pytest.approx(sheet.width_mm, abs=0.5)
    assert spec.grid.columns == sheet.columns
    assert spec.paragraph_styles, "the base template's styles are kept"
    assert not spec.master_pages, "the base's furniture is sized for its own sheet"
    assert spec.meta["harvested_from"]
    body = next(style for style in spec.paragraph_styles if style.id == "body")
    assert body.font_family, "a measurement cannot see the font, so the base's is kept"


def test_the_measured_sizes_reach_the_template(system: DesignSystem, template) -> None:
    spec = system.to_template(base=template)

    roles = system.role_sizes()
    body = next(style for style in spec.paragraph_styles if style.id == "body")
    assert body.size_pt == pytest.approx(roles["body"], abs=0.2)
    assert body.leading_pt > body.size_pt


def test_the_system_survives_being_written_and_read(system: DesignSystem, tmp_path: Path) -> None:
    path = system.save(tmp_path / "system.json")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["columns"] == system.columns
    assert payload["furniture"]
    assert payload["confidence"]


def test_what_it_is_unsure_of_is_said_rather_than_hidden(system: DesignSystem) -> None:
    assert set(system.confidence) >= {"sheet", "grid", "type", "furniture"}
    assert all(0.0 <= value <= 1.0 for value in system.confidence.values())


def test_a_publication_that_cannot_be_read_says_so(tmp_path: Path) -> None:
    from app.harvest.reader import HarvestError

    with pytest.raises(HarvestError):
        PublicationHarvester().harvest_pdf(tmp_path / "nothing.pdf")
