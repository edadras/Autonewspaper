"""Page composition strategies.

A strategy decides *where* stories sit on a page; it never decides what type
they are set in. Each strategy partitions the live area into one clean,
non-overlapping rectangle per story, snapped to the column grid, so a page can
never be produced with stories on top of each other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.layout.geometry import distribute, guillotine, split_horizontal, split_vertical
from app.layout.grid import GridSystem
from app.models.schemas import AreaKind, Rect

log = logging.getLogger(__name__)


@dataclass
class ImageSlot:
    """The picture attached to a story, if any."""

    asset_id: int | None = None
    path: str = ""
    aspect: float = 1.5
    quality: float = 60.0
    caption: str = ""
    generated: bool = False


@dataclass
class ArticleBlock:
    """Everything the layout engine needs to know about one story."""

    article_id: int
    headline: str = ""
    subtitle: str = ""
    kicker: str = ""
    byline: str = ""
    lead: str = ""
    body: str = ""
    quote: str = ""
    weight: float = 1.0
    area: AreaKind = AreaKind.SECONDARY
    priority: int = 50
    image: ImageSlot | None = None
    column_span: int = 0
    """Preferred span; ``0`` lets the strategy decide."""
    continued: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        """Words in the body copy."""
        from app.utils.text import word_count

        return word_count(self.body)


@dataclass
class Region:
    """A story and the rectangle allocated to it."""

    block: ArticleBlock
    rect: Rect


Strategy = Callable[[GridSystem, Rect, list[ArticleBlock], int], list[Region]]


def block_payload(block: ArticleBlock) -> dict[str, Any]:
    """Serialise a block so a page can be recomposed after QA."""
    payload = {
        "article_id": block.article_id,
        "headline": block.headline,
        "subtitle": block.subtitle,
        "kicker": block.kicker,
        "byline": block.byline,
        "lead": block.lead,
        "body": block.body,
        "quote": block.quote,
        "weight": block.weight,
        "area": block.area.value,
        "priority": block.priority,
        "column_span": block.column_span,
        "continued": block.continued,
        "meta": dict(block.meta),
        "image": None,
    }
    if block.image is not None:
        payload["image"] = {
            "asset_id": block.image.asset_id,
            "path": block.image.path,
            "aspect": block.image.aspect,
            "quality": block.image.quality,
            "caption": block.image.caption,
            "generated": block.image.generated,
        }
    return payload


def block_from_payload(payload: dict[str, Any]) -> ArticleBlock:
    """Rebuild a block from :func:`block_payload`."""
    data = dict(payload)
    image = data.pop("image", None)
    area = data.pop("area", "secondary")
    block = ArticleBlock(**data)
    block.area = AreaKind(area) if area in {a.value for a in AreaKind} else AreaKind.SECONDARY
    if image:
        block.image = ImageSlot(**image)
    return block


def _resolve_overlap(rect: Rect, placed: Rect, gutter: float) -> Rect:
    """Shrink *rect* just enough to clear *placed*, cutting the shorter way."""
    overlap = rect.intersection(placed)
    if overlap.width <= 0.4 or overlap.height <= 0.4:
        return rect
    # Four possible cuts; pick the one that costs the least area.
    options: list[Rect] = []
    if placed.bottom <= rect.bottom:  # placed is above -> cut from the top
        new_y = placed.bottom + gutter
        options.append(Rect(x=rect.x, y=new_y, width=rect.width, height=rect.bottom - new_y))
    if placed.y >= rect.y:  # placed is below -> cut from the bottom
        options.append(Rect(x=rect.x, y=rect.y, width=rect.width, height=placed.y - gutter - rect.y))
    if placed.right <= rect.right:  # placed is to the left -> cut from the left
        new_x = placed.right + gutter
        options.append(Rect(x=new_x, y=rect.y, width=rect.right - new_x, height=rect.height))
    if placed.x >= rect.x:  # placed is to the right -> cut from the right
        options.append(Rect(x=rect.x, y=rect.y, width=placed.x - gutter - rect.x, height=rect.height))
    valid = [r for r in options if r.width > 1.0 and r.height > 1.0]
    if not valid:
        return Rect(x=rect.x, y=rect.y, width=0.0, height=0.0)
    return max(valid, key=lambda r: r.area)


def _snap_regions(grid: GridSystem, regions: list[Region], gutter: float) -> list[Region]:
    """Snap every region to the column grid, guaranteeing no overlap.

    Regions are placed in order. Each one is snapped to the grid and then
    clipped against everything already placed, so snapping can never make two
    stories collide. A region that would be clipped away entirely keeps its
    original, unsnapped rectangle clipped the same way.
    """
    placed: list[Rect] = []
    out: list[Region] = []
    min_width = max(12.0, grid.column_width * 0.6)
    min_height = 12.0

    for region in regions:
        original = grid.clamp(region.rect)
        candidate = grid.snap_rect(original, snap_baseline=False)
        for existing in placed:
            candidate = _resolve_overlap(candidate, existing, gutter)
            if candidate.width <= 0 or candidate.height <= 0:
                break
        if candidate.width < min_width or candidate.height < min_height:
            candidate = original
            for existing in placed:
                candidate = _resolve_overlap(candidate, existing, gutter)
                if candidate.width <= 0 or candidate.height <= 0:
                    break
        if candidate.width < 6.0 or candidate.height < 6.0:
            log.debug(
                "Dropping region for article %s: no space left on the page", region.block.article_id
            )
            continue
        candidate = grid.clamp(candidate)
        placed.append(candidate)
        out.append(Region(block=region.block, rect=candidate))
    return out



def allocate_columns(weights: list[float], total_columns: int, minimum: int = 1) -> list[int]:
    """Split *total_columns* into integer spans proportional to *weights*.

    Uses the largest-remainder method with a per-story minimum, so column
    based strategies land exactly on grid boundaries instead of being snapped
    afterwards (which is what used to make neighbouring columns collide).
    """
    count = len(weights)
    if count == 0:
        return []
    if count * minimum >= total_columns:
        return [max(1, total_columns // count)] * count
    total = sum(max(1e-6, w) for w in weights)
    spare = total_columns - count * minimum
    exact = [max(1e-6, w) / total * spare for w in weights]
    spans = [minimum + int(value) for value in exact]
    remainder = total_columns - sum(spans)
    order = sorted(range(count), key=lambda i: -(exact[i] - int(exact[i])))
    for index in range(remainder):
        spans[order[index % count]] += 1
    return spans


def _weights(blocks: list[ArticleBlock]) -> list[float]:
    """Normalised area weights derived from priority, area kind and length."""
    area_bonus = {
        AreaKind.MAIN: 2.4,
        AreaKind.SECONDARY: 1.35,
        AreaKind.SMALL: 0.85,
        AreaKind.SIDEBAR: 0.7,
    }
    out: list[float] = []
    for block in blocks:
        weight = block.weight if block.weight > 0 else 1.0
        weight *= area_bonus.get(block.area, 1.0)
        weight *= 0.6 + block.priority / 100.0
        weight *= 1.0 + min(0.6, block.word_count / 900.0)
        if block.image is not None:
            weight *= 1.12
        out.append(max(0.15, weight))
    return out


# ------------------------------------------------------------- strategies --


def hierarchical(grid: GridSystem, area: Rect, blocks: list[ArticleBlock], variant: int = 0) -> list[Region]:
    """Classic front page: a dominant lead across the top, the rest below."""
    if not blocks:
        return []
    gutter = grid.gutter_mm
    if len(blocks) == 1:
        return _snap_regions(grid, [Region(blocks[0], area)], gutter)

    lead_share = [0.46, 0.54, 0.40, 0.60, 0.50][variant % 5]
    top, bottom = split_horizontal(area, lead_share, gutter)
    regions = [Region(blocks[0], top)]

    rest = blocks[1:]
    if len(rest) == 1:
        regions.append(Region(rest[0], bottom))
    else:
        orientations = ["vertical", "horizontal", "vertical", "horizontal"]
        if variant % 2:
            orientations = ["horizontal", "vertical", "horizontal", "vertical"]
        rects = guillotine(bottom, _weights(rest), orientations, gutter)
        regions.extend(Region(block, rect) for block, rect in zip(rest, rects))
    return _snap_regions(grid, regions, gutter)


def modular(grid: GridSystem, area: Rect, blocks: list[ArticleBlock], variant: int = 0) -> list[Region]:
    """Even modular grid: every story is a rectangle of the same visual family."""
    if not blocks:
        return []
    starts = [
        ["vertical", "horizontal"],
        ["horizontal", "vertical"],
        ["vertical", "vertical", "horizontal"],
        ["horizontal", "horizontal", "vertical"],
    ]
    orientations = starts[variant % len(starts)]
    rects = guillotine(area, _weights(blocks), orientations, grid.gutter_mm)
    return _snap_regions(
        grid, [Region(block, rect) for block, rect in zip(blocks, rects)], grid.gutter_mm
    )


def horizontal(grid: GridSystem, area: Rect, blocks: list[ArticleBlock], variant: int = 0) -> list[Region]:
    """Horizontal bands stacked down the page."""
    if not blocks:
        return []
    weights = _weights(blocks)
    if variant % 2:
        weights = [w * (1.25 if index == 0 else 0.95) for index, w in enumerate(weights)]
    rects = distribute(area, weights, "horizontal", grid.gutter_mm)
    return _snap_regions(
        grid, [Region(block, rect) for block, rect in zip(blocks, rects)], grid.gutter_mm
    )


def vertical(grid: GridSystem, area: Rect, blocks: list[ArticleBlock], variant: int = 0) -> list[Region]:
    """Full-height columns, the traditional dense news page.

    Column spans are allocated as whole grid columns so the regions land
    exactly on the grid. When there are more stories than columns the page is
    first divided into horizontal bands, each of which is then columned.
    """
    if not blocks:
        return []
    if len(blocks) > grid.columns:
        band_count = -(-len(blocks) // grid.columns)
        chunks = [blocks[i::band_count] for i in range(band_count)]
        chunks = [chunk for chunk in chunks if chunk]
        bands = distribute(
            area, [sum(_weights(chunk)) for chunk in chunks], "horizontal", grid.gutter_mm
        )
        regions: list[Region] = []
        for chunk, band in zip(chunks, bands):
            regions.extend(_columns_in(grid, band, chunk))
        return _snap_regions(grid, regions, grid.gutter_mm)

    regions = _columns_in(grid, area, blocks)
    if variant % 2 and len(regions) > 2:
        # Split the widest column in two so the page is not a picket fence.
        widest = max(range(len(regions)), key=lambda i: regions[i].rect.width)
        top, bottom = split_horizontal(regions[widest].rect, 0.55, grid.gutter_mm)
        moved = regions.pop()
        regions[widest] = Region(regions[widest].block, top)
        regions.append(Region(moved.block, bottom))
    return _snap_regions(grid, regions, grid.gutter_mm)


def _columns_in(grid: GridSystem, area: Rect, blocks: list[ArticleBlock]) -> list[Region]:
    """Lay *blocks* out side by side across whole grid columns of *area*."""
    spans = allocate_columns(_weights(blocks), grid.columns)
    start_column = grid.nearest_column_index(area.x)
    available = grid.columns - start_column
    regions: list[Region] = []
    cursor = start_column
    for block, span in zip(blocks, spans):
        remaining = start_column + available - cursor
        if remaining <= 0:
            break
        span = max(1, min(span, remaining))
        regions.append(
            Region(
                block,
                Rect(
                    x=grid.column_x(cursor),
                    y=area.y,
                    width=grid.span_width(span),
                    height=area.height,
                ),
            )
        )
        cursor += span
    return regions


def feature(grid: GridSystem, area: Rect, blocks: list[ArticleBlock], variant: int = 0) -> list[Region]:
    """One picture-led feature with a narrow rail of shorter items."""
    if not blocks:
        return []
    gutter = grid.gutter_mm
    if len(blocks) == 1:
        return _snap_regions(grid, [Region(blocks[0], area)], gutter)

    rail_share = [0.28, 0.32, 0.24][variant % 3]
    if grid.direction == "rtl":
        main_rect, rail = split_vertical(area, 1.0 - rail_share, gutter)
    else:
        rail, main_rect = split_vertical(area, rail_share, gutter)

    regions = [Region(blocks[0], main_rect)]
    rest = blocks[1:]
    rail_rects = distribute(rail, _weights(rest), "horizontal", gutter)
    regions.extend(Region(block, rect) for block, rect in zip(rest, rail_rects))
    return _snap_regions(grid, regions, gutter)


STRATEGIES: dict[str, Strategy] = {
    "hierarchical": hierarchical,
    "modular": modular,
    "horizontal": horizontal,
    "vertical": vertical,
    "feature": feature,
}


def build_candidates(
    grid: GridSystem,
    area: Rect,
    blocks: list[ArticleBlock],
    allowed: list[str],
    count: int,
) -> list[tuple[str, int, list[Region]]]:
    """Produce up to *count* distinct ``(strategy, variant, regions)`` candidates."""
    names = [name for name in allowed if name in STRATEGIES] or list(STRATEGIES)
    out: list[tuple[str, int, list[Region]]] = []
    variant = 0
    while len(out) < count:
        for name in names:
            if len(out) >= count:
                break
            regions = STRATEGIES[name](grid, area, blocks, variant)
            if regions:
                out.append((name, variant, regions))
        variant += 1
        if variant > count + 4:
            break
    return out
