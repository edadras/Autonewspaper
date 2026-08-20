"""Geometry, grid, typography and scoring internals."""

from __future__ import annotations

import pytest

from app.layout.geometry import (
    alignment_score,
    coverage,
    distribute,
    find_gaps,
    guillotine,
    split_horizontal,
    split_vertical,
    total_overlap_area,
)
from app.layout.grid import GridSystem
from app.layout.scoring import LayoutScorer
from app.layout.strategies import STRATEGIES, allocate_columns, build_candidates
from app.layout.typography import TypographyEngine
from app.models.schemas import ElementType, Rect


def test_splits_preserve_the_area_minus_the_gap():
    rect = Rect(x=0, y=0, width=100, height=200)
    top, bottom = split_horizontal(rect, 0.4, gap=4)
    assert top.height + bottom.height == pytest.approx(196)
    assert bottom.y == pytest.approx(top.bottom + 4)
    left, right = split_vertical(rect, 0.25, gap=4)
    assert left.width + right.width == pytest.approx(96)


def test_guillotine_never_overlaps():
    area = Rect(x=14, y=14, width=271, height=390)
    for count in range(1, 9):
        rects = guillotine(area, [1.0] * count, ["horizontal", "vertical"], 4)
        assert len(rects) == count
        assert total_overlap_area(rects) == 0.0


def test_distribute_respects_the_weights():
    rects = distribute(Rect(x=0, y=0, width=300, height=100), [3, 1], "vertical", 0)
    assert rects[0].width == pytest.approx(225)
    assert rects[1].width == pytest.approx(75)


def test_coverage_counts_overlapping_frames_once():
    area = Rect(x=0, y=0, width=100, height=100)
    a = Rect(x=0, y=0, width=60, height=100)
    b = Rect(x=40, y=0, width=60, height=100)
    assert coverage([a, b], area) == pytest.approx(1.0, abs=0.02)


def test_find_gaps_reports_a_large_empty_band():
    area = Rect(x=0, y=0, width=100, height=100)
    gaps = find_gaps([Rect(x=0, y=0, width=100, height=45)], area)
    assert gaps
    assert gaps[0].ratio > 0.4


def test_alignment_score_prefers_shared_edges():
    aligned = [Rect(x=0, y=0, width=50, height=20), Rect(x=0, y=25, width=50, height=20)]
    ragged = [Rect(x=0, y=0, width=50, height=20), Rect(x=7, y=27, width=43, height=19)]
    assert alignment_score(aligned) > alignment_score(ragged)


def test_allocate_columns_sums_to_the_grid():
    for weights in ([1, 1, 1], [5, 1], [3, 2, 2, 1], [1] * 6):
        spans = allocate_columns(weights, 6)
        assert sum(spans) == 6
        assert all(span >= 1 for span in spans)


def test_grid_snapping_lands_on_a_column(template):
    grid = GridSystem.from_template(template, 1)
    snapped = grid.snap_rect(Rect(x=21.4, y=63.2, width=131.0, height=97.0), snap_baseline=False)
    unit = grid.column_width + grid.gutter_mm
    offset = (snapped.x - grid.content.x) % unit
    assert min(offset, unit - offset) < 0.01


def test_rtl_reading_column_mirrors(template):
    grid = GridSystem.from_template(template, 1)
    assert grid.direction == "rtl"
    assert grid.reading_column(0) == grid.columns - 1


def test_typography_shrinks_before_it_truncates(template):
    engine = TypographyEngine(template)
    rect = Rect(x=0, y=0, width=90, height=40)
    result = engine.fit("متن خبر " * 90, rect, ElementType.BODY, columns=2, allow_truncate=True)
    assert result.typography.size_pt <= template.style_for(ElementType.BODY).size_pt
    assert result.overflow == 0.0


def test_display_type_fills_its_frame(template):
    engine = TypographyEngine(template)
    small = engine.fit_display("تیتر", Rect(x=0, y=0, width=80, height=14))
    large = engine.fit_display("تیتر", Rect(x=0, y=0, width=260, height=40))
    assert large.typography.size_pt > small.typography.size_pt


def test_measure_overflow_uses_the_given_size(template):
    engine = TypographyEngine(template)
    typography = engine.resolve(ElementType.BODY)
    tight = Rect(x=0, y=0, width=60, height=10)
    overflow, lines = engine.measure_overflow("کلمه " * 200, tight, typography)
    assert overflow > 0
    assert lines > 1


def test_every_strategy_produces_candidates(template, article_blocks):
    grid = GridSystem.from_template(template, 1)
    candidates = build_candidates(grid, grid.content, article_blocks, list(STRATEGIES), 10)
    assert len(candidates) >= 5
    for name, _variant, regions in candidates:
        assert name in STRATEGIES
        assert total_overlap_area([r.rect for r in regions]) == 0.0


def test_scorer_penalises_overlap(template, article_blocks):
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, candidates_per_page=3)
    plan = engine.plan_edition(1, {1: article_blocks}, page_count=1)
    page = plan.pages[0]
    clean = LayoutScorer(template).score(page)

    movable = [e for e in page.elements if not e.locked][:2]
    movable[1].rect = movable[0].rect.model_copy()
    broken = LayoutScorer(template).score(page)
    assert broken.total < clean.total
    assert broken.penalties["overlap"] > 0
