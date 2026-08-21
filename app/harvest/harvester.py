"""Reducing a run of pages to the design system behind them.

One page is a sample and can be atypical - a front page carries a masthead,
a picture spread carries no columns at all. The system is what the pages
agree on, so every figure here is a median across the run and every figure
carries how much of the run agreed with it.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from app.core.jobs import CancelToken
from app.design.furniture import Furniture, FurnitureSpec
from app.harvest.analysis import PageMeasurements, Panel, measure_page, type_scale
from app.harvest.publication import DesignSystem, HarvestedPage
from app.harvest.reader import PageRaster, read_images, read_pdf

log = logging.getLogger(__name__)

#: Panels this alike are the same box appearing on several pages, and are
#: reported once. In millimetres, because a designer draws to the millimetre.
SAME_PANEL_MM = 6.0

#: A box that appears on this share of the pages is part of the paper's
#: furniture rather than one story's decoration.
RECURRING_SHARE = 0.34


class PublicationHarvester:
    """Measures a publication and reports the system behind it."""

    def __init__(self, *, dpi: int = 110, max_pages: int = 16) -> None:
        self.dpi = dpi
        self.max_pages = max_pages

    # -------------------------------------------------------------- entry
    def harvest_pdf(
        self,
        path: Path | str,
        *,
        name: str = "",
        pages: list[int] | None = None,
        token: CancelToken | None = None,
    ) -> DesignSystem:
        """Read a publication that arrived as a PDF."""
        rasters = read_pdf(path, dpi=self.dpi, pages=pages, limit=self.max_pages)
        return self._reduce(rasters, name=name or Path(path).stem, source=Path(path).name, token=token)

    def harvest_images(
        self,
        paths: list[Path | str],
        *,
        width_mm: float,
        name: str = "",
        token: CancelToken | None = None,
    ) -> DesignSystem:
        """Read a publication that arrived as page images."""
        rasters = read_images(paths, width_mm=width_mm, limit=self.max_pages)
        source = Path(paths[0]).parent.name if paths else "images"
        return self._reduce(rasters, name=name or source, source=source, token=token)

    # ------------------------------------------------------------ reduce
    def _reduce(
        self,
        rasters: list[PageRaster],
        *,
        name: str,
        source: str,
        token: CancelToken | None = None,
    ) -> DesignSystem:
        measured: list[PageMeasurements] = []
        try:
            for raster in rasters:
                if token is not None and token.cancelled:
                    log.info("Harvest cancelled after %d page(s)", len(measured))
                    break
                measured.append(measure_page(raster))
        finally:
            for raster in rasters:
                raster.close()
        if not measured:
            from app.harvest.reader import HarvestError

            raise HarvestError(f"No page of {source} could be measured")

        system = DesignSystem(name=name, source=source)
        system.pages = [HarvestedPage(item) for item in measured]
        self._sheet(system, measured)
        self._grid(system, measured)
        self._colours(system, measured)
        self._type(system, measured)
        self._furniture(system, measured)
        system.notes.extend(
            note for item in measured for note in item.notes if note not in system.notes
        )
        log.info("Harvested %s: %s", source, system.describe())
        return system

    # ------------------------------------------------------------- sheet
    def _sheet(self, system: DesignSystem, pages: list[PageMeasurements]) -> None:
        """The trim size and the margins the run agrees on."""
        system.page_width_mm = statistics.median(page.width_mm for page in pages)
        system.page_height_mm = statistics.median(page.height_mm for page in pages)
        sizes = Counter((page.width_mm, page.height_mm) for page in pages)
        system.confidence["sheet"] = sizes.most_common(1)[0][1] / len(pages)
        if len(sizes) > 1:
            system.notes.append(
                f"The pages are not all one size ({len(sizes)} different); the most common "
                "one was taken as the sheet"
            )

        # A margin is a limit, not an average: it is how close to the trim the
        # type is ever allowed to come. Taking the median instead gives a
        # magazine whose lower two thirds is a photograph a bottom margin of
        # a quarter of a metre, which is true of that page and true of no
        # page anybody would set. The low percentile keeps it a boundary
        # while staying proof against one odd page.
        body = [page for page in pages if page.ink_share > 0.02]
        if not body:
            body = pages
        system.margin_top_mm = _limit([page.margin_top_mm for page in body])
        system.margin_bottom_mm = _limit([page.margin_bottom_mm for page in body])
        left = _limit([page.margin_left_mm for page in body])
        right = _limit([page.margin_right_mm for page in body])

        # Facing pages alternate, so the inside margin is the one that differs
        # between odd and even pages; when they do not differ, both are the same.
        odd = [page for page in body if page.index % 2 == 1]
        even = [page for page in body if page.index % 2 == 0]
        if len(odd) >= 2 and len(even) >= 2:
            odd_left = _limit([page.margin_left_mm for page in odd])
            even_left = _limit([page.margin_left_mm for page in even])
            if abs(odd_left - even_left) > 3.0:
                system.margin_inside_mm = max(odd_left, even_left)
                system.margin_outside_mm = min(odd_left, even_left)
                system.notes.append(
                    "The left margin differs between odd and even pages, so the paper is "
                    "set on facing pages with a wider inside margin"
                )
                return
        system.margin_inside_mm = left
        system.margin_outside_mm = right

    # -------------------------------------------------------------- grid
    def _grid(self, system: DesignSystem, pages: list[PageMeasurements]) -> None:
        """The column grid, taken as the one most pages are set on.

        A single measure counts as an answer. A magazine set to one wide
        column on five pages out of six is not a six-column paper because one
        page of it happened to fit a six-column grid, and reporting it as one
        would lay every future page out wrong.
        """
        counts = Counter(page.columns for page in pages)
        columns, agreed = counts.most_common(1)[0]
        matching = [page for page in pages if page.columns == columns]
        system.columns = max(1, columns)
        system.gutter_mm = round(_typical([page.gutter_mm for page in matching]), 1)
        system.confidence["grid"] = agreed / len(pages)
        if columns <= 1:
            system.notes.append(
                "This publication is set to a single measure rather than to a column grid"
            )
            system.gutter_mm = 0.0
            return
        if agreed < len(pages) * 0.5:
            runners = [f"{count} on {seen} page(s)" for count, seen in counts.most_common(3)]
            system.notes.append(
                f"The pages do not agree on a grid ({', '.join(runners)}); the most common "
                "one was taken"
            )

    # ----------------------------------------------------------- colours
    def _colours(self, system: DesignSystem, pages: list[PageMeasurements]) -> None:
        """The paper, the ink and the accent this publication prints in."""
        from app.creative.style import contrast_ratio, readable_on

        system.paper = Counter(page.paper for page in pages).most_common(1)[0][0]
        seen: Counter[str] = Counter()
        for page in pages:
            seen.update(page.palette)
        system.palette = [colour for colour, _count in seen.most_common(8)]

        darkest = min(
            (colour for colour in system.palette),
            key=lambda colour: _luminance(colour),
            default="#111111",
        )
        system.ink = darkest
        # The accent is the most saturated colour that is neither the paper
        # nor the ink - the section red a paper flags its furniture with.
        candidates = [
            colour
            for colour in system.palette
            if colour not in (system.paper, system.ink) and _saturation(colour) > 0.25
        ]
        if candidates:
            system.accent = max(candidates, key=_saturation)
        elif contrast_ratio(system.paper, "#c2410c") >= 3.0:
            system.accent = "#c2410c"
        else:
            system.accent = readable_on(system.paper)

    # -------------------------------------------------------------- type
    def _type(self, system: DesignSystem, pages: list[PageMeasurements]) -> None:
        """The sizes this publication sets, and the rules it draws."""
        bands = [band for page in pages for band in page.bands]
        system.sizes_pt = type_scale(bands)
        system.confidence["type"] = min(1.0, len(bands) / (len(pages) * 40))
        weights = [rule.weight_pt for page in pages for rule in page.rules]
        if weights:
            counted = Counter(round(weight * 4) / 4 for weight in weights)
            system.rule_weights_pt = [
                weight for weight, _count in counted.most_common(4) if weight > 0
            ]
        if not system.sizes_pt:
            system.notes.append(
                "No lines of type could be measured; the sizes are the base template's"
            )

    # --------------------------------------------------------- furniture
    def _furniture(self, system: DesignSystem, pages: list[PageMeasurements]) -> None:
        """The boxes this paper draws, ready to be redrawn in Photoshop.

        Panels are grouped across pages, because the same sidebar box on six
        pages is one piece of furniture, not six; and a box that recurs is
        reported ahead of one that appeared once, because that is the one the
        paper is actually built from.
        """
        groups: list[list[Panel]] = []
        for page in pages:
            for panel in page.panels:
                for group in groups:
                    if _alike(panel, group[0]):
                        group.append(panel)
                        break
                else:
                    groups.append([panel])

        groups.sort(key=lambda group: (len(group), group[0].area_mm2), reverse=True)
        total = max(1, len(pages))
        out: list[FurnitureSpec] = []
        for group in groups[:12]:
            sample = group[0]
            recurs = len(group) / total
            out.append(
                FurnitureSpec(
                    kind=_kind_of(sample),
                    width_mm=round(statistics.median(item.width_mm for item in group), 1),
                    height_mm=round(statistics.median(item.height_mm for item in group), 1),
                    dpi=300,
                    color=sample.color,
                    accent=system.accent,
                    ink=system.ink,
                    corner_mm=sample.corner_mm,
                    rule_pt=round(sample.keyline_pt or 0.75, 2),
                    seed=f"{sample.color}@{len(group)}",
                )
            )
            if recurs >= RECURRING_SHARE:
                log.info(
                    "%s appears on %d of %d pages; treating it as the paper's own furniture",
                    out[-1].kind.value,
                    len(group),
                    total,
                )
        system.furniture = out
        system.confidence["furniture"] = min(1.0, len(out) / 6)


def _kind_of(panel: Panel) -> Furniture:
    """Which piece of furniture a measured box is.

    A ruled box and a tint panel are drawn differently, and a wide shallow
    band across a page is a section bar rather than either.
    """
    if panel.keyline:
        if panel.holds_a_picture:
            return Furniture.PHOTO_FRAME
        if panel.corner_mm >= 1.5:
            return Furniture.SHADOW_CARD
        return Furniture.RULED_BOX
    if panel.height_mm <= 18.0 and panel.width_mm >= panel.height_mm * 4:
        return Furniture.SECTION_BAR
    if panel.corner_mm >= 1.5:
        return Furniture.SHADOW_CARD
    return Furniture.TINT_PANEL


def _alike(left: Panel, right: Panel) -> bool:
    """Whether two measured boxes are the same piece of furniture."""
    from app.harvest.analysis import _close

    return (
        abs(left.width_mm - right.width_mm) <= SAME_PANEL_MM
        and abs(left.height_mm - right.height_mm) <= SAME_PANEL_MM
        and left.keyline == right.keyline
        and left.holds_a_picture == right.holds_a_picture
        and _close(left.color, right.color, 30)
    )


def _typical(values: list[float]) -> float:
    """The middle of a set of measurements, ignoring the extremes.

    A trimmed median: one page that bleeds a picture off the trim should not
    move the paper's margin, and neither should one that is nearly blank.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) >= 5:
        cut = len(ordered) // 5
        ordered = ordered[cut : len(ordered) - cut] or ordered
    return round(statistics.median(ordered), 1)


def _limit(values: list[float], percentile: float = 0.2) -> float:
    """How close to the trim a measurement ever comes, ignoring the outlier.

    The low percentile rather than the minimum: one page bleeding a picture
    off the edge should not set the paper's margin to zero, and one page of
    solid type should not set it to the widest thing on the run either.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) < 3:
        return round(ordered[0], 1)
    index = min(len(ordered) - 1, max(0, int(round(percentile * (len(ordered) - 1)))))
    return round(ordered[index], 1)


def _luminance(colour: str) -> float:
    """How light a colour is, for picking the ink out of a palette."""
    from app.creative.style import relative_luminance

    try:
        return relative_luminance(colour)
    except Exception:  # noqa: BLE001 - a malformed swatch sorts last
        return 1.0


def _saturation(colour: str) -> float:
    """How coloured a colour is, for picking the accent out of a palette."""
    try:
        bands = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    except (ValueError, IndexError):
        return 0.0
    high, low = max(bands), min(bands)
    return 0.0 if high <= 0 else (high - low) / high


def describe_confidence(system: DesignSystem) -> dict[str, Any]:
    """What the harvest is sure of and what it is not."""
    return {
        "sheet": system.confidence.get("sheet", 0.0),
        "grid": system.confidence.get("grid", 0.0),
        "type": system.confidence.get("type", 0.0),
        "furniture": system.confidence.get("furniture", 0.0),
        "notes": system.notes,
    }
