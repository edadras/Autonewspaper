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
