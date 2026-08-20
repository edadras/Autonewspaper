"""The layout guarantees of specification §44.

Every generated page must satisfy these regardless of the template, the number
of stories or the strategy the engine chose.
"""

from __future__ import annotations

import pytest

from app.layout.engine import LayoutEngine
from app.layout.geometry import coverage, total_overlap_area
from app.layout.grid import GridSystem
from app.models.schemas import ElementType, IssueType, Rect
from app.utils.units import effective_dpi


@pytest.fixture
def plan(template, article_blocks):
    engine = LayoutEngine(template, candidates_per_page=6)
    return engine.plan_edition(
        1,
        {1: article_blocks},
        page_count=1,
        publication_name="روزنامه صبح",
        edition_date="1405/05/29",
        asset_quality={1: 82.0, 2: 82.0},
        asset_pixels={1: (2000, 1200), 2: (2000, 1200)},
    )


def test_no_frames_overlap(plan):
    for page in plan.pages:
        rects = [e.rect for e in page.elements if e.type is not ElementType.RULE]
        assert total_overlap_area(rects, tolerance=0.5) == 0.0, f"page {page.index} has overlap"


def test_every_frame_is_inside_the_page(plan):
    for page in plan.pages:
        for element in page.elements:
            assert page.page_rect.contains(element.rect, tolerance=0.5), (
                f"{element.frame_name} leaves page {page.index}"
            )


def test_running_text_respects_the_margins(plan):
    for page in plan.pages:
        content = page.content_rect
        for element in page.elements:
            if element.locked or not element.is_text:
                continue
            assert content.contains(element.rect, tolerance=0.8), (
                f"{element.frame_name} intrudes into the margin of page {page.index}"
            )


def test_bleed_is_carried_into_the_plan(plan, template):
    for page in plan.pages:
        assert page.bleed_mm == template.bleed_mm


def test_no_text_frame_overflows(plan):
    for page in plan.pages:
        for element in page.elements:
            assert element.estimated_overflow <= 0.001, (
                f"{element.frame_name} overflows by {element.estimated_overflow:.0%}"
            )


def test_typography_never_falls_below_the_template_minimum(plan, template):
    minimum = template.layout_rules.min_body_size_pt
    for page in plan.pages:
        for element in page.elements:
            if element.typography is None:
                continue
            floor = (
                template.layout_rules.headline_min_size_pt
                if element.type in (ElementType.HEADLINE, ElementType.MASTHEAD)
                else minimum
            )
            assert element.typography.size_pt >= floor - 0.01, (
                f"{element.frame_name} is set at {element.typography.size_pt} pt"
            )


def test_all_text_is_right_to_left_in_a_persian_template(plan):
    for page in plan.pages:
        for element in page.elements:
            if element.typography is None or not element.is_text:
                continue
            assert element.typography.direction == "rtl"
            assert element.typography.alignment != "left"


def test_english_template_produces_left_to_right_text(english_template, article_blocks):
    engine = LayoutEngine(english_template, candidates_per_page=4)
    plan = engine.plan_edition(1, {1: article_blocks[:3]}, page_count=1)
    for element in plan.pages[0].elements:
        if element.typography is not None and element.is_text:
            assert element.typography.direction == "ltr"
            assert element.typography.alignment != "right"


def test_placed_images_print_above_the_minimum_resolution(plan, template):
    minimum = template.layout_rules.min_image_dpi
    for page in plan.pages:
        for element in page.elements:
            if not element.is_image or element.asset_id is None:
                continue
            dpi = effective_dpi(2000, element.rect.width)
            assert dpi >= minimum, f"{element.frame_name} would print at {dpi:.0f} dpi"


def test_frames_sit_on_the_column_grid(plan, template):
    grid = GridSystem.from_template(template, 1)
    unit = grid.column_width + grid.gutter_mm
    for page in plan.pages:
        for element in page.elements:
            if element.locked or element.type in (ElementType.RULE, ElementType.FOLIO):
                continue
            offset = (element.rect.x - page.content_rect.x) % unit
            assert min(offset, unit - offset) <= 1.6


def test_the_page_is_neither_empty_nor_overfull(plan):
    for page in plan.pages:
        covered = coverage([e.rect for e in page.elements], page.content_rect)
        assert 0.45 <= covered <= 1.0


def test_hierarchy_is_visible_in_the_headline_sizes(plan):
    headlines = sorted(
        (
            e.typography.size_pt
            for page in plan.pages
            for e in page.elements
            if e.type is ElementType.HEADLINE and e.typography and not e.locked
        ),
        reverse=True,
    )
    assert len(headlines) >= 2
    assert headlines[0] > headlines[1] + 1.0


def test_constraint_checker_agrees_the_page_is_clean(plan, template):
    from app.layout.constraints import ConstraintChecker

    checker = ConstraintChecker(template)
    for page in plan.pages:
        report = checker.check(page, asset_pixels={1: (2000, 1200), 2: (2000, 1200)})
        blocking = [v for v in report.violations if v.hard]
        assert not blocking, [v.message for v in blocking]


def test_a_deliberately_broken_page_is_detected(plan, template):
    from app.layout.constraints import ConstraintChecker

    page = plan.pages[0]
    movable = [e for e in page.elements if not e.locked and e.is_text][0]
    movable.rect = Rect(x=-30.0, y=movable.rect.y, width=movable.rect.width, height=movable.rect.height)
    report = ConstraintChecker(template).check(page)
    kinds = {v.type for v in report.violations}
    assert IssueType.OUT_OF_BOUNDS in kinds or IssueType.MARGIN_VIOLATION in kinds


# ----------------------------------------------- rules that must not misfire
def test_a_template_cannot_contradict_its_own_minimums(template):
    """A style allowed below the rule its template declares is a design fault.

    Two of the shipped templates carried one, and every page they produced was
    reported as faulty for furniture the operator could not change.
    """
    from app.templates.schema import TemplateSpec

    payload = template.model_dump(mode="json")
    body = next(style for style in payload["paragraph_styles"] if style["id"] == "body")
    body["min_size_pt"] = payload["layout_rules"]["min_body_size_pt"] - 1.0

    with pytest.raises(ValueError, match="below this template"):
        TemplateSpec.model_validate(payload)


def test_every_shipped_template_is_internally_consistent():
    from pathlib import Path

    from app.templates.schema import TemplateSpec

    root = Path(__file__).resolve().parents[2] / "templates"
    files = sorted(root.glob("*.template.json"))
    assert files
    for path in files:
        TemplateSpec.load(path)  # raises when a style breaks its own rules


def test_a_folio_may_be_smaller_than_body_text(template):
    """Folios, captions and kickers are meant to be set small."""
    from app.layout.constraints import SECONDARY_TEXT

    for style_id in SECONDARY_TEXT:
        style = template.paragraph_style(style_id)
        if style is None:
            continue
        assert style.min_size_pt >= template.layout_rules.min_secondary_size_pt
    folio = template.paragraph_style("folio")
    assert folio is not None
    assert folio.size_pt < template.paragraph_style("body").size_pt


def test_a_picture_exactly_on_the_minimum_is_not_reported(template, article_blocks):
    """Reporting "200 dpi (minimum 200)" is noise, not information."""
    from app.layout.constraints import ConstraintChecker

    engine = LayoutEngine(template, language="fa")
    plan = engine.plan_edition(1, {1: article_blocks}, page_count=1)
    page = plan.pages[0]
    image = next((e for e in page.elements if e.is_image), None)
    assert image is not None

    minimum = template.layout_rules.min_image_dpi
    # Size the source so the frame prints at exactly the minimum.
    image.meta["source_width_px"] = int(round(image.rect.width / 25.4 * minimum))
    image.meta["source_height_px"] = int(round(image.rect.height / 25.4 * minimum))

    report = ConstraintChecker(template).check(page)
    low = [v for v in report.violations if v.type is IssueType.LOW_IMAGE_RESOLUTION]
    assert low == [], [v.message for v in low]


def test_a_page_number_in_a_full_width_folio_is_not_called_blank(english_template, article_blocks, tmp_path):
    """The magazine folio holds one digit in a band the width of the page.

    Judging blankness as a fraction of the frame reported every such page as
    having lost its folio text, at high severity, on a page that was correct.
    """
    from app.vision.analyzer import PageAnalyzer
    from app.vision.renderer import PreviewRenderer

    engine = LayoutEngine(english_template, language="en")
    plan = engine.plan_edition(
        1,
        {1: article_blocks[:2], 2: article_blocks[2:]},
        page_count=2,
        publication_name="The Morning",
        edition_date="2024-08-20",
    )
    page = plan.pages[1]
    folio = next(e for e in page.elements if e.type is ElementType.FOLIO)
    assert len(folio.text.strip()) <= 3, "this test is about a folio holding just a page number"

    preview = PreviewRenderer(english_template, dpi=110).render_page(page, tmp_path / "page.png")
    analyzer = PageAnalyzer(110)
    metrics = analyzer.analyze_pixels(preview.path, page)

    measured = metrics.element_ink[folio.id]
    assert measured > 0, "the folio rendered nothing at all"
    issues = analyzer.issues_from_pixels(metrics, page)
    blank = [i for i in issues if i.type is IssueType.EMPTY_FRAME and i.element_id == folio.id]
    assert blank == [], f"folio ink {measured:.5f} was called blank"


def test_a_frame_that_really_rendered_nothing_is_still_caught(template, article_blocks, tmp_path):
    from app.vision.analyzer import PageAnalyzer
    from app.vision.renderer import PreviewRenderer

    engine = LayoutEngine(template, language="fa")
    plan = engine.plan_edition(1, {1: article_blocks}, page_count=1)
    page = plan.pages[0]
    body = next(e for e in page.elements if e.type is ElementType.BODY)

    preview = PreviewRenderer(template, dpi=110).render_page(page, tmp_path / "page.png")
    analyzer = PageAnalyzer(110)
    metrics = analyzer.analyze_pixels(preview.path, page)
    metrics.element_ink[body.id] = 0.0  # as if the frame had come back empty

    issues = analyzer.issues_from_pixels(metrics, page)
    assert any(i.type is IssueType.EMPTY_FRAME and i.element_id == body.id for i in issues)
