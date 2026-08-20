"""The layout engine.

Turns editorial input into a fully specified, constraint-satisfying
:class:`~app.models.schemas.LayoutPlan`:

1. stories are grouped per page and converted into :class:`ArticleBlock` s;
2. every strategy produces several candidate partitions of the live area;
3. each region is filled with real frames by the typography engine;
4. candidates are checked against the constraints and scored;
5. the best candidate wins, and stories that did not fit move to the next page.

The plan is geometry only - InDesign remains the source of truth for the
finished document (specification §52).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from app.layout.constraints import ConstraintChecker, ConstraintReport
from app.layout.grid import GridSystem
from app.layout.scoring import LayoutScore, LayoutScorer
from app.layout.strategies import (
    ArticleBlock,
    ImageSlot,
    Region,
    block_payload,
    build_candidates,
)
from app.layout.typography import TypographyEngine
from app.models.schemas import (
    AreaKind,
    ElementSpec,
    ElementType,
    LayoutPlan,
    PageLayout,
    Rect,
)
from app.templates.schema import MasterPageSpec, TemplateSpec
from app.utils.units import closest_aspect_ratio, pt_to_mm

log = logging.getLogger(__name__)

#: Share of the headline style's maximum size each editorial area may use.
HEADLINE_SIZE_CAP: dict[AreaKind, float] = {
    AreaKind.MAIN: 1.0,
    AreaKind.SECONDARY: 0.62,
    AreaKind.SMALL: 0.42,
    AreaKind.SIDEBAR: 0.36,
}


@dataclass
class PageCandidate:
    """One scored candidate for a page."""

    page: PageLayout
    strategy: str
    variant: int
    score: LayoutScore
    report: ConstraintReport
    placed_articles: int
    dropped_articles: list[int]

    @property
    def effective_score(self) -> float:
        """Score after penalising stories the candidate could not place."""
        return self.score.total - 9.0 * len(self.dropped_articles)


@dataclass
class PagePlan:
    """Result of planning one page."""

    page: PageLayout
    candidates: list[PageCandidate]
    overflow: list[ArticleBlock]
    """Stories that did not fit and must move to the next page."""


class LayoutEngine:
    """Generates and scores page layouts for a template."""

    def __init__(
        self,
        template: TemplateSpec,
        *,
        language: str | None = None,
        candidates_per_page: int = 6,
        seed: int = 20240101,
    ) -> None:
        self.template = template
        self.language = language or template.language
        self.typography = TypographyEngine(template, self.language)
        self.checker = ConstraintChecker(template)
        self.scorer = LayoutScorer(template)
        self.candidates_per_page = max(1, candidates_per_page)
        self.random = random.Random(seed)

    # ------------------------------------------------------------- edition
    def plan_edition(
        self,
        project_id: int,
        blocks_by_page: dict[int, list[ArticleBlock]],
        *,
        page_count: int,
        project_slug: str = "",
        design_style: str = "classic",
        publication_name: str = "",
        edition_date: str = "",
        sections: dict[int, str] | None = None,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
    ) -> LayoutPlan:
        """Plan every page, carrying overflow stories forward."""
        sections = sections or {}
        pages: list[PageLayout] = []
        carry: list[ArticleBlock] = []

        for index in range(1, page_count + 1):
            blocks = carry + list(blocks_by_page.get(index, []))
            carry = []
            if not blocks and index > 1:
                # Keep the page in the document but leave it empty; the QA
                # stage reports it so the operator can drop the page or add copy.
                pages.append(self._empty_page(index, sections.get(index, ""), publication_name, edition_date))
                continue
            plan = self.plan_page(
                index,
                blocks,
                section=sections.get(index, ""),
                publication_name=publication_name,
                edition_date=edition_date,
                page_count=page_count,
                asset_quality=asset_quality,
                asset_pixels=asset_pixels,
            )
            pages.append(plan.page)
            if plan.overflow and index < page_count:
                carry = plan.overflow
            elif plan.overflow:
                log.warning(
                    "%d story/stories could not be placed in the edition: %s",
                    len(plan.overflow),
                    [b.article_id for b in plan.overflow],
                )

        plan = LayoutPlan(
            project_id=project_id,
            project_slug=project_slug,
            template_id=self.template.id,
            design_style=design_style,
            language=self.language,
            pages=pages,
        )
        plan.score = self.scorer.score_plan(pages)
        plan.meta["unplaced_articles"] = [b.article_id for b in carry]
        return plan

    # ---------------------------------------------------------------- page
    def plan_page(
        self,
        index: int,
        blocks: list[ArticleBlock],
        *,
        section: str = "",
        publication_name: str = "",
        edition_date: str = "",
        page_count: int = 1,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
    ) -> PagePlan:
        """Generate, score and select the best layout for one page."""
        grid = GridSystem.from_template(self.template, index)
        master = self.template.master_for(index)
        master_elements = self._master_elements(
            master, index, publication_name, edition_date, section, page_count
        )
        area = self._usable_area(grid, master_elements)

        ordered = self._order_blocks(blocks)
        capacity = self.template.layout_rules.max_articles_per_page
        placed_blocks, overflow = ordered[:capacity], ordered[capacity:]

        candidates: list[PageCandidate] = []
        allowed = self.template.layout_rules.allowed_strategies
        for strategy, variant, regions in build_candidates(
            grid, area, placed_blocks, allowed, self.candidates_per_page
        ):
            page = self._compose(index, grid, regions, master_elements, section, strategy, variant)
            report = self.checker.check(page, asset_pixels=asset_pixels)
            score = self.scorer.score(page, report, asset_quality=asset_quality)
            page.score = score.total
            page.score_breakdown = score.breakdown()
            placed_ids = {r.block.article_id for r in regions}
            dropped = [b.article_id for b in placed_blocks if b.article_id not in placed_ids]
            candidates.append(
                PageCandidate(
                    page=page,
                    strategy=strategy,
                    variant=variant,
                    score=score,
                    report=report,
                    placed_articles=len(placed_ids),
                    dropped_articles=dropped,
                )
            )

        if not candidates:
            empty = self._empty_page(index, section, publication_name, edition_date)
            return PagePlan(page=empty, candidates=[], overflow=blocks)

        candidates.sort(key=lambda c: -c.effective_score)
        best = candidates[0]
        best.page.meta.update(
            {
                "strategy": best.strategy,
                "variant": best.variant,
                "candidates": [
                    {"strategy": c.strategy, "variant": c.variant, "score": round(c.effective_score, 2)}
                    for c in candidates
                ],
                "violations": [v.to_dict() for v in best.report.violations],
                "dropped_articles": best.dropped_articles,
            }
        )
        dropped_blocks = [b for b in placed_blocks if b.article_id in set(best.dropped_articles)]
        log.info(
            "Page %d: %s/%d selected with score %.1f (%d candidate(s), %d article(s))",
            index,
            best.strategy,
            best.variant,
            best.score.total,
            len(candidates),
            best.placed_articles,
        )
        return PagePlan(page=best.page, candidates=candidates, overflow=dropped_blocks + overflow)

    # ------------------------------------------------------------ assembly
    def _order_blocks(self, blocks: list[ArticleBlock]) -> list[ArticleBlock]:
        """Sort stories so the lead comes first, then by priority."""
        rank = {AreaKind.MAIN: 0, AreaKind.SECONDARY: 1, AreaKind.SMALL: 2, AreaKind.SIDEBAR: 3}
        return sorted(blocks, key=lambda b: (rank.get(b.area, 2), -b.priority, b.article_id))

    def _usable_area(self, grid: GridSystem, master_elements: list[ElementSpec]) -> Rect:
        """Live area minus the space reserved by master-page furniture."""
        area = grid.content
        top = area.y
        bottom = area.bottom
        for element in master_elements:
            rect = element.rect
            if rect.bottom <= area.y + area.height * 0.35 and rect.width > area.width * 0.5:
                top = max(top, rect.bottom + grid.gutter_mm)
            elif rect.y >= area.y + area.height * 0.65 and rect.width > area.width * 0.5:
                bottom = min(bottom, rect.y - grid.gutter_mm)
        return Rect(x=area.x, y=top, width=area.width, height=max(20.0, bottom - top))

    def _master_elements(
        self,
        master: MasterPageSpec | None,
        page_index: int,
        publication_name: str,
        edition_date: str,
        section: str,
        page_count: int,
    ) -> list[ElementSpec]:
        """Instantiate the master page furniture for this page."""
        if master is None:
            return []
        out: list[ElementSpec] = []
        substitutions = {
            "{publication_name}": publication_name,
            "{edition_date}": edition_date,
            "{page_number}": str(page_index),
            "{page_count}": str(page_count),
            "{section}": section,
        }
        for order, spec in enumerate(master.elements):
            if spec.only_pages == "odd" and page_index % 2 == 0:
                continue
            if spec.only_pages == "even" and page_index % 2 == 1:
                continue
            if spec.only_pages == "first" and page_index != 1:
                continue
            text = spec.text
            for token, value in substitutions.items():
                text = text.replace(token, value)
            rect = Rect(x=spec.x_mm, y=spec.y_mm, width=spec.width_mm, height=spec.height_mm)
            element = ElementSpec(
                id=f"m{page_index}_{order}",
                type=spec.type,
                rect=rect,
                z_index=-10 + order,
                text=text,
                style_id=spec.style_id or spec.type.value,
                image_path=spec.image_path,
                locked=True,
                frame_name=f"master_{spec.type.value}_{order}",
                meta={"master": master.name},
            )
            if element.is_text and text:
                fit = (
                    self.typography.fit_display(text, rect, spec.type, max_lines=2)
                    if spec.type in (ElementType.MASTHEAD,)
                    else self.typography.fit(text, rect, spec.type)
                )
                element.typography = fit.typography
                element.text = fit.text
                element.estimated_overflow = fit.overflow
            out.append(element)
        return out

    def _compose(
        self,
        index: int,
        grid: GridSystem,
        regions: list[Region],
        master_elements: list[ElementSpec],
        section: str,
        strategy: str,
        variant: int,
    ) -> PageLayout:
        """Build the full :class:`PageLayout` for one candidate."""
        top, bottom, left, right = self.template.margins_for(index)
        page = PageLayout(
            index=index,
            width_mm=self.template.page_width_mm,
            height_mm=self.template.page_height_mm,
            margin_top_mm=top,
            margin_bottom_mm=bottom,
            margin_inside_mm=left,
            margin_outside_mm=right,
            bleed_mm=self.template.bleed_mm,
            columns=self.template.grid.columns,
            gutter_mm=self.template.grid.gutter_mm,
            section=section,
            master=(self.template.master_for(index).name if self.template.master_for(index) else "A-Master"),
            candidate_id=f"{strategy}-{variant}",
        )
        page.elements.extend(master_elements)
        for order, region in enumerate(regions):
            page.elements.extend(self._fill_region(page, grid, region, order))
        # Keep the source stories so QA can recompose the page with another
        # strategy without going back to the database.
        page.meta["blocks"] = [block_payload(region.block) for region in regions]
        return page

    def _fill_region(
        self, page: PageLayout, grid: GridSystem, region: Region, order: int
    ) -> list[ElementSpec]:
        """Lay out one story inside its rectangle."""
        block = region.block
        rect = region.rect
        prefix = f"a{block.article_id}_{page.index}_{order}"
        elements: list[ElementSpec] = []
        cursor = rect.y
        remaining = rect.height
        gutter = 1.6

        def _take(height: float) -> Rect | None:
            nonlocal cursor, remaining
            height = min(height, remaining)
            if height < 3.0:
                return None
            taken = Rect(x=rect.x, y=cursor, width=rect.width, height=height)
            cursor += height + gutter
            remaining -= height + gutter
            return taken

        # --- kicker -------------------------------------------------------
        if block.kicker:
            band = _take(self.typography.height_for(block.kicker, rect.width, ElementType.KICKER))
            if band:
                elements.append(
                    self._text_element(f"{prefix}_kicker", ElementType.KICKER, band, block, block.kicker)
                )

        # --- headline -----------------------------------------------------
        headline_lines = {AreaKind.MAIN: 3, AreaKind.SECONDARY: 3, AreaKind.SMALL: 2, AreaKind.SIDEBAR: 2}
        max_lines = headline_lines.get(block.area, 2)
        headline_share = {
            AreaKind.MAIN: 0.26,
            AreaKind.SECONDARY: 0.24,
            AreaKind.SMALL: 0.28,
            AreaKind.SIDEBAR: 0.26,
        }
        headline_height = min(
            remaining * headline_share.get(block.area, 0.25),
            pt_to_mm(self.typography.style_spec(ElementType.HEADLINE).max_size_pt * 1.15) * max_lines,
        )
        band = _take(max(headline_height, 10.0))
        if band and block.headline:
            # Cap the display size by editorial weight so a secondary story can
            # never be set as large as the lead.
            headline_spec = self.typography.style_spec(ElementType.HEADLINE)
            cap = headline_spec.max_size_pt * HEADLINE_SIZE_CAP.get(block.area, 0.5)
            fit = self.typography.fit_display(
                block.headline, band, ElementType.HEADLINE, max_lines=max_lines, size_cap_pt=cap
            )
            used = min(band.height, max(10.0, fit.lines * pt_to_mm(fit.typography.leading_pt) * 1.06))
            band = Rect(x=band.x, y=band.y, width=band.width, height=used)
            cursor = band.bottom + gutter
            remaining = rect.bottom - cursor
            elements.append(
                ElementSpec(
                    id=f"{prefix}_headline",
                    type=ElementType.HEADLINE,
                    rect=band,
                    z_index=order * 10 + 2,
                    article_id=block.article_id,
                    text=fit.text,
                    style_id="headline",
                    typography=fit.typography,
                    estimated_overflow=fit.overflow,
                    meta={
                        "truncated": fit.truncated,
                        "used_min_size": fit.used_min_size,
                        "area": block.area.value,
                    },
                )
            )

        # --- deck ---------------------------------------------------------
        if block.subtitle and block.area in (AreaKind.MAIN, AreaKind.SECONDARY) and remaining > 24:
            height = self.typography.height_for(block.subtitle, rect.width, ElementType.SUBHEADLINE)
            band = _take(min(height, remaining * 0.18))
            if band:
                elements.append(
                    self._text_element(f"{prefix}_deck", ElementType.SUBHEADLINE, band, block, block.subtitle)
                )

        # --- byline -------------------------------------------------------
        if block.byline and remaining > 18:
            band = _take(self.typography.height_for(block.byline, rect.width, ElementType.BYLINE))
            if band:
                elements.append(
                    self._text_element(f"{prefix}_byline", ElementType.BYLINE, band, block, block.byline)
                )

        # --- picture ------------------------------------------------------
        if block.image is not None and remaining > 30:
            image_share = {
                AreaKind.MAIN: 0.46,
                AreaKind.SECONDARY: 0.40,
                AreaKind.SMALL: 0.34,
                AreaKind.SIDEBAR: 0.30,
            }.get(block.area, 0.38)
            ideal = rect.width / max(0.4, block.image.aspect)
            height = min(remaining * image_share, ideal, remaining - 18.0)
            band = _take(max(0.0, height))
            if band and band.height >= 14.0:
                elements.append(
                    ElementSpec(
                        id=f"{prefix}_image",
                        type=ElementType.IMAGE,
                        rect=band,
                        z_index=order * 10 + 1,
                        article_id=block.article_id,
                        asset_id=block.image.asset_id,
                        image_path=block.image.path,
                        style_id="image_frame",
                        fit_mode="fill",
                        meta={
                            "aspect": round(band.aspect, 3),
                            "requested_aspect": closest_aspect_ratio(band.width, max(1e-6, band.height)),
                            "ai_generated": block.image.generated,
                        },
                    )
                )
                caption = block.image.caption
                if caption and remaining > 16:
                    caption_band = _take(self.typography.height_for(caption, rect.width, ElementType.CAPTION))
                    if caption_band:
                        elements.append(
                            self._text_element(
                                f"{prefix}_caption", ElementType.CAPTION, caption_band, block, caption
                            )
                        )

        # --- lead ---------------------------------------------------------
        columns = max(1, min(grid.columns_for_width(rect.width), 4))
        if block.lead and remaining > 22:
            height = self.typography.height_for(block.lead, rect.width, ElementType.LEAD, columns)
            band = _take(min(height, remaining * 0.32))
            if band:
                elements.append(
                    self._text_element(
                        f"{prefix}_lead", ElementType.LEAD, band, block, block.lead, columns=columns
                    )
                )

        # --- pull quote ----------------------------------------------------
        if block.quote and block.area is AreaKind.MAIN and remaining > 60:
            band = _take(min(24.0, remaining * 0.16))
            if band:
                elements.append(
                    self._text_element(f"{prefix}_quote", ElementType.QUOTE, band, block, block.quote)
                )

        # --- body ----------------------------------------------------------
        if remaining >= 10.0:
            available = max(0.0, rect.bottom - cursor)
            element_kind = ElementType.SIDEBAR if block.area is AreaKind.SIDEBAR else ElementType.BODY
            columns_probe = max(1, min(grid.columns_for_width(rect.width), 4))
            needed = self.typography.height_for(block.body, rect.width, element_kind, columns_probe)
            # A frame taller than its copy would hide the white space it leaves;
            # size it to the text (plus a little slack) so QA sees the gap.
            band = Rect(
                x=rect.x,
                y=cursor,
                width=rect.width,
                height=min(available, max(needed * 1.04, 8.0)) if needed else available,
            )
            if band.height >= 8.0:
                element_type = element_kind
                fit = self.typography.fit(
                    block.body, band, element_type, columns=columns, allow_truncate=True
                )
                elements.append(
                    ElementSpec(
                        id=f"{prefix}_body",
                        type=element_type,
                        rect=band,
                        z_index=order * 10,
                        article_id=block.article_id,
                        text=fit.text,
                        style_id=element_type.value,
                        typography=fit.typography,
                        estimated_overflow=fit.overflow,
                        column_span=columns,
                        meta={
                            "truncated": fit.truncated,
                            "used_min_size": fit.used_min_size,
                            "full_word_count": block.word_count,
                        },
                    )
                )
        return elements

    def _text_element(
        self,
        element_id: str,
        element_type: ElementType,
        rect: Rect,
        block: ArticleBlock,
        text: str,
        *,
        columns: int = 1,
    ) -> ElementSpec:
        """Build a fitted text frame."""
        fit = self.typography.fit(text, rect, element_type, columns=columns, allow_truncate=True)
        return ElementSpec(
            id=element_id,
            type=element_type,
            rect=rect,
            z_index=1,
            article_id=block.article_id,
            text=fit.text,
            style_id=element_type.value,
            typography=fit.typography,
            estimated_overflow=fit.overflow,
            column_span=columns,
            meta={"truncated": fit.truncated, "used_min_size": fit.used_min_size},
        )

    def _empty_page(self, index: int, section: str, publication_name: str, edition_date: str) -> PageLayout:
        """A page with only its master furniture."""
        top, bottom, left, right = self.template.margins_for(index)
        master = self.template.master_for(index)
        page = PageLayout(
            index=index,
            width_mm=self.template.page_width_mm,
            height_mm=self.template.page_height_mm,
            margin_top_mm=top,
            margin_bottom_mm=bottom,
            margin_inside_mm=left,
            margin_outside_mm=right,
            bleed_mm=self.template.bleed_mm,
            columns=self.template.grid.columns,
            gutter_mm=self.template.grid.gutter_mm,
            section=section,
            master=master.name if master else "A-Master",
        )
        page.elements.extend(
            self._master_elements(master, index, publication_name, edition_date, section, index)
        )
        page.meta["empty"] = True
        return page

    # --------------------------------------------------------- re-scoring
    def rescore(
        self,
        page: PageLayout,
        *,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
    ) -> tuple[LayoutScore, ConstraintReport]:
        """Re-check and re-score a page after it has been modified."""
        report = self.checker.check(page, asset_pixels=asset_pixels)
        score = self.scorer.score(page, report, asset_quality=asset_quality)
        page.score = score.total
        page.score_breakdown = score.breakdown()
        page.meta["violations"] = [v.to_dict() for v in report.violations]
        return score, report

    def refit(self, page: PageLayout) -> PageLayout:
        """Recompute the reported overflow of every frame of *page*.

        Called after a correction moved, resized or re-sized a frame. Type that
        has already been resolved (or deliberately changed by a correction) is
        kept and only re-measured; frames that never got typography are fitted
        from scratch.
        """
        for element in page.elements:
            if not element.is_text or not element.text:
                continue
            columns = max(1, element.column_span)
            if element.typography is not None:
                overflow, _lines = self.typography.measure_overflow(
                    element.text, element.rect, element.typography
                )
                element.estimated_overflow = overflow
                minimum = self.typography.style_spec(element.type).min_size_pt
                element.meta["used_min_size"] = element.typography.size_pt <= minimum + 0.01
                continue
            fit = self.typography.fit(
                element.text, element.rect, element.type, columns=columns, allow_truncate=False
            )
            element.typography = fit.typography
            element.estimated_overflow = fit.overflow
            element.meta.update({"used_min_size": fit.used_min_size})
        return page

    def describe(self) -> dict[str, Any]:
        """Diagnostics for the layout inspector."""
        return {
            "template": self.template.id,
            "language": self.language,
            "candidates_per_page": self.candidates_per_page,
            "typography": self.typography.describe(),
            "strategies": self.template.layout_rules.allowed_strategies,
        }


def make_image_slot(
    asset_id: int | None,
    path: str,
    width: int,
    height: int,
    quality: float,
    caption: str = "",
    generated: bool = False,
) -> ImageSlot:
    """Convenience constructor used by the pipeline."""
    aspect = (width / height) if height else 1.5
    return ImageSlot(
        asset_id=asset_id,
        path=path,
        aspect=round(aspect, 4),
        quality=quality,
        caption=caption,
        generated=generated,
    )
