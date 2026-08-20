"""Geometric primitives used by the layout engine.

Everything here works on :class:`~app.models.schemas.Rect` in millimetres and
is deliberately free of any layout policy - policy lives in
:mod:`app.layout.strategies`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.models.schemas import Rect

Orientation = Literal["horizontal", "vertical"]


def split_horizontal(rect: Rect, ratio: float, gap: float = 0.0) -> tuple[Rect, Rect]:
    """Split *rect* into a top and a bottom part; *ratio* is the top share."""
    ratio = max(0.05, min(0.95, ratio))
    usable = max(0.0, rect.height - gap)
    top_height = usable * ratio
    top = Rect(x=rect.x, y=rect.y, width=rect.width, height=top_height)
    bottom = Rect(
        x=rect.x,
        y=rect.y + top_height + gap,
        width=rect.width,
        height=max(0.0, usable - top_height),
    )
    return top, bottom


def split_vertical(rect: Rect, ratio: float, gap: float = 0.0) -> tuple[Rect, Rect]:
    """Split *rect* into a left and a right part; *ratio* is the left share."""
    ratio = max(0.05, min(0.95, ratio))
    usable = max(0.0, rect.width - gap)
    left_width = usable * ratio
    left = Rect(x=rect.x, y=rect.y, width=left_width, height=rect.height)
    right = Rect(
        x=rect.x + left_width + gap,
        y=rect.y,
        width=max(0.0, usable - left_width),
        height=rect.height,
    )
    return left, right


def stack_vertical(rect: Rect, heights: Iterable[float], gap: float = 0.0) -> list[Rect]:
    """Stack bands of the given heights from the top of *rect*."""
    out: list[Rect] = []
    cursor = rect.y
    for height in heights:
        out.append(Rect(x=rect.x, y=cursor, width=rect.width, height=max(0.0, height)))
        cursor += height + gap
    return out


def distribute(rect: Rect, weights: list[float], orientation: Orientation, gap: float = 0.0) -> list[Rect]:
    """Divide *rect* into ``len(weights)`` parts proportional to *weights*."""
    weights = [max(0.001, w) for w in weights] or [1.0]
    total = sum(weights)
    count = len(weights)
    out: list[Rect] = []
    if orientation == "horizontal":
        usable = max(0.0, rect.height - gap * (count - 1))
        cursor = rect.y
        for weight in weights:
            height = usable * weight / total
            out.append(Rect(x=rect.x, y=cursor, width=rect.width, height=height))
            cursor += height + gap
    else:
        usable = max(0.0, rect.width - gap * (count - 1))
        cursor = rect.x
        for weight in weights:
            width = usable * weight / total
            out.append(Rect(x=cursor, y=rect.y, width=width, height=rect.height))
            cursor += width + gap
    return out


def guillotine(
    rect: Rect,
    weights: list[float],
    orientations: list[Orientation],
    gap: float = 0.0,
) -> list[Rect]:
    """Recursively cut *rect* into one region per weight.

    At each level the region is cut in two along ``orientations[depth]`` with
    the split point chosen so each half receives its share of the total
    weight. This produces classic newspaper "modular" layouts in which every
    story occupies a clean rectangle and no story overlaps another.
    """
    if not weights:
        return []
    if len(weights) == 1:
        return [rect]
    orientation = orientations[0] if orientations else "horizontal"
    rest = orientations[1:] or [("vertical" if orientation == "horizontal" else "horizontal")]

    pivot = _balanced_pivot(weights)
    head, tail = weights[:pivot], weights[pivot:]
    ratio = sum(head) / sum(weights)
    if orientation == "horizontal":
        first, second = split_horizontal(rect, ratio, gap)
    else:
        first, second = split_vertical(rect, ratio, gap)
    return guillotine(first, head, rest, gap) + guillotine(second, tail, rest, gap)


def _balanced_pivot(weights: list[float]) -> int:
    """Index that splits *weights* into the two most balanced halves."""
    total = sum(weights)
    running = 0.0
    best_index, best_delta = 1, float("inf")
    for index in range(1, len(weights)):
        running += weights[index - 1]
        delta = abs(running - total / 2.0)
        if delta < best_delta:
            best_delta, best_index = delta, index
    return best_index


def inset_rect(rect: Rect, top: float = 0, right: float = 0, bottom: float = 0, left: float = 0) -> Rect:
    """Shrink *rect* by independent per-side amounts."""
    return Rect(
        x=rect.x + left,
        y=rect.y + top,
        width=max(0.0, rect.width - left - right),
        height=max(0.0, rect.height - top - bottom),
    )


def clamp_into(rect: Rect, bounds: Rect) -> Rect:
    """Move and shrink *rect* so it fits inside *bounds*."""
    width = min(rect.width, bounds.width)
    height = min(rect.height, bounds.height)
    x = min(max(rect.x, bounds.x), bounds.right - width)
    y = min(max(rect.y, bounds.y), bounds.bottom - height)
    return Rect(x=x, y=y, width=width, height=height)


def total_overlap_area(rects: list[Rect], tolerance: float = 0.2) -> float:
    """Sum of pairwise overlapping area (0 for a clean modular layout)."""
    total = 0.0
    for index, first in enumerate(rects):
        for second in rects[index + 1 :]:
            intersection = first.intersection(second)
            if intersection.width > tolerance and intersection.height > tolerance:
                total += intersection.area
    return total


def coverage(rects: list[Rect], area: Rect) -> float:
    """Fraction of *area* covered by *rects*, computed without double counting.

    Uses a coordinate-compressed sweep so overlapping frames are only counted
    once - the whitespace metric would otherwise be meaningless.
    """
    if area.area <= 0 or not rects:
        return 0.0
    clipped = [r.intersection(area) for r in rects]
    clipped = [r for r in clipped if r.area > 0]
    if not clipped:
        return 0.0
    xs = sorted({r.x for r in clipped} | {r.right for r in clipped})
    ys = sorted({r.y for r in clipped} | {r.bottom for r in clipped})
    covered = 0.0
    for xi in range(len(xs) - 1):
        x0, x1 = xs[xi], xs[xi + 1]
        if x1 - x0 <= 0:
            continue
        for yi in range(len(ys) - 1):
            y0, y1 = ys[yi], ys[yi + 1]
            if y1 - y0 <= 0:
                continue
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if any(r.x <= cx <= r.right and r.y <= cy <= r.bottom for r in clipped):
                covered += (x1 - x0) * (y1 - y0)
    return min(1.0, covered / area.area)


@dataclass
class Gap:
    """An empty rectangular region left on a page."""

    rect: Rect
    ratio: float
    """Share of the live area this gap occupies."""


def find_gaps(rects: list[Rect], area: Rect, min_ratio: float = 0.04) -> list[Gap]:
    """Locate empty bands large enough to count as excessive white space.

    Scans the live area row by row on the column grid and merges adjacent
    fully-empty cells into maximal rectangles; only gaps above *min_ratio* of
    the page are reported, which is what the QA stage flags.
    """
    if area.area <= 0:
        return []
    steps_x, steps_y = 24, 32
    cell_w, cell_h = area.width / steps_x, area.height / steps_y
    occupied = [[False] * steps_x for _ in range(steps_y)]
    for row in range(steps_y):
        for col in range(steps_x):
            cx = area.x + (col + 0.5) * cell_w
            cy = area.y + (row + 0.5) * cell_h
            occupied[row][col] = any(r.x <= cx <= r.right and r.y <= cy <= r.bottom for r in rects)

    gaps: list[Gap] = []
    seen = [[False] * steps_x for _ in range(steps_y)]
    for row in range(steps_y):
        for col in range(steps_x):
            if occupied[row][col] or seen[row][col]:
                continue
            width = 0
            while col + width < steps_x and not occupied[row][col + width] and not seen[row][col + width]:
                width += 1
            height = 1
            while row + height < steps_y and all(
                not occupied[row + height][c] and not seen[row + height][c] for c in range(col, col + width)
            ):
                height += 1
            for r in range(row, row + height):
                for c in range(col, col + width):
                    seen[r][c] = True
            rect = Rect(
                x=area.x + col * cell_w,
                y=area.y + row * cell_h,
                width=width * cell_w,
                height=height * cell_h,
            )
            ratio = rect.area / area.area
            if ratio >= min_ratio:
                gaps.append(Gap(rect=rect, ratio=round(ratio, 4)))
    return sorted(gaps, key=lambda g: -g.ratio)


def alignment_score(rects: list[Rect], tolerance: float = 1.5) -> float:
    """0..1 score rewarding shared edges (the visual grid of a good page)."""
    if len(rects) < 2:
        return 1.0
    edges: list[float] = []
    for rect in rects:
        edges.extend([rect.x, rect.right, rect.y, rect.bottom])
    aligned = 0
    for index, value in enumerate(edges):
        for other in edges[index + 1 :]:
            if abs(value - other) <= tolerance:
                aligned += 1
                break
    return min(1.0, aligned / max(1, len(edges) * 0.55))


def center_of_mass(rects: list[Rect]) -> tuple[float, float]:
    """Area-weighted centre of a set of rectangles."""
    total = sum(r.area for r in rects)
    if total <= 0:
        return (0.0, 0.0)
    x = sum(r.center[0] * r.area for r in rects) / total
    y = sum(r.center[1] * r.area for r in rects) / total
    return (x, y)
