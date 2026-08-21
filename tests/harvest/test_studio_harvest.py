"""Working in a publication's own idiom.

The point of measuring a paper is to lay the next page out the way that paper
lays pages out - on its sheet, its grid, its type scale, with its own boxes
copied rather than reinvented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.studio_tools import StudioContext, build_page_tools
from app.harvest.publication import DesignSystem
from tests.harvest.conftest import Sheet, draw_page


@pytest.fixture(scope="session")
def daily(publication: list[Path], sheet_spec: Sheet) -> DesignSystem:
    """One paper, measured once for the whole file."""
    from app.harvest.harvester import PublicationHarvester

    return PublicationHarvester().harvest_images(
        publication, width_mm=sheet_spec.width_mm, name="daily"
    )


@pytest.fixture
def measured(studio_context: StudioContext, daily: DesignSystem):
    """A studio that has read one paper."""
    studio_context.systems["daily"] = daily
    return build_page_tools(studio_context), daily


def test_a_document_takes_the_publications_own_measurements(measured, sheet: Sheet) -> None:
    registry, system = measured

    result = registry.invoke("start_document", {"name": "tomorrow", "like": "daily"})

    assert result.ok, result.error
    assert result.data["page_width_mm"] == pytest.approx(sheet.width_mm, abs=0.5)
    assert result.data["columns"] == sheet.columns
    assert result.data["like"] == "daily"
    assert result.data["type_sizes_pt"] == system.sizes_pt


def test_the_type_is_set_in_the_publications_own_sizes(measured) -> None:
    registry, system = measured
    registry.invoke("start_document", {"name": "tomorrow", "like": "daily"})

    result = registry.invoke(
        "add_text_frame",
        {"document": "tomorrow", "page": 1, "name": "head", "text": "سرخط",
         "x_mm": 20, "y_mm": 25, "width_mm": 150, "height_mm": 30, "style": "headline"},
    )

    assert result.data["size_pt"] == pytest.approx(system.role_sizes()["headline"], abs=0.3)


def test_a_named_size_wins_over_the_measured_sheet(measured) -> None:
    """An operator putting a paper's design onto A3 has a reason."""
    registry, _system = measured

    result = registry.invoke(
        "start_document", {"name": "onto_a3", "like": "daily", "format": "A3"}
    )

    assert result.data["page_width_mm"] == pytest.approx(297.0, abs=0.5)
    assert result.data["columns"] == 6, "the paper's grid survives the change of sheet"


def test_the_margins_scale_with_a_change_of_sheet(measured, sheet: Sheet) -> None:
    registry, _system = measured
    same = registry.invoke("start_document", {"name": "same", "like": "daily"})
    smaller = registry.invoke("start_document", {"name": "small", "like": "daily", "format": "A5"})

    assert smaller.data["live_area_mm"]["x"] < same.data["live_area_mm"]["x"]


def test_a_box_is_copied_from_the_publication(measured, tmp_path: Path) -> None:
    """This is what was asked for: copying the paper's boxes, not inventing some."""
    registry, system = measured
    registry.invoke("start_document", {"name": "tomorrow", "like": "daily"})
    kind = system.furniture[0].kind.value

    result = registry.invoke(
        "add_page_furniture",
        {"document": "tomorrow", "page": 1, "name": "aside", "kind": kind,
         "x_mm": 200, "y_mm": 90, "width_mm": 60, "height_mm": 140, "like": "daily"},
    )

    assert result.ok, result.error
    assert result.data["copied_from"] == "daily"
    assert result.data["color"] == system.furniture[0].color
    assert Path(result.data["path"]).exists()
    # The frame is the size that was asked for; the piece is drawn a little
    # larger and placed back out by the same amount, so an edge effect is not
    # clipped. That overhang is the bleed and belongs there.
    assert 60.0 <= result.data["width_mm"] <= 60.0 + 6.0


def test_a_box_the_publication_never_drew_is_refused(measured) -> None:
    registry, system = measured
    registry.invoke("start_document", {"name": "tomorrow", "like": "daily"})
    absent = next(
        kind
        for kind in ("quote_plate", "drop_cap_plate", "flag_marker")
        if kind not in {piece.kind.value for piece in system.furniture}
    )

    result = registry.invoke(
        "add_page_furniture",
        {"document": "tomorrow", "page": 1, "name": "x", "kind": absent,
         "x_mm": 20, "y_mm": 20, "width_mm": 60, "height_mm": 40, "like": "daily"},
    )

    assert not result.ok
    assert "has no" in result.error


def test_asking_to_work_like_a_paper_nobody_measured_says_so(studio_context) -> None:
    registry = build_page_tools(studio_context)

    result = registry.invoke("start_document", {"name": "x", "like": "nowhere"})

    assert not result.ok
    assert "measured" in result.error


def test_the_harvest_tool_reads_a_pdf_end_to_end(studio_context, tmp_path: Path, sheet: Sheet) -> None:
    """The tool takes a PDF, because that is what an issue arrives as."""
    pytest.importorskip("pypdf")
    import shutil

    if shutil.which("pdftoppm") is None:
        pytest.skip("poppler is not installed")


    pages = [draw_page(sheet, seed=index) for index in range(1, 4)]
    source = tmp_path / "issue.pdf"
    pages[0].save(source, save_all=True, append_images=pages[1:], resolution=110.0)

    registry = build_page_tools(studio_context)
    result = registry.invoke("harvest_publication", {"path": str(source), "name": "issue", "pages": 3})

    assert result.ok, result.error
    assert result.data["columns"] == sheet.columns
    assert result.data["page_width_mm"] == pytest.approx(sheet.width_mm, abs=1.0)
    assert Path(result.data["path"]).exists()
    assert "describes" in result.data


def test_something_that_is_not_a_publication_is_refused(studio_context, photograph) -> None:
    registry = build_page_tools(studio_context)

    result = registry.invoke("harvest_publication", {"path": str(photograph)})

    assert not result.ok
    assert "not a PDF" in result.error


def test_what_was_measured_can_be_read_back(measured) -> None:
    registry, _system = measured

    result = registry.invoke("describe_system", {"system": "daily"})

    assert result.ok
    assert result.data["columns"]
    assert result.data["furniture"]
    assert isinstance(result.data["pages"], int), "the per-page readings are counted, not dumped"
