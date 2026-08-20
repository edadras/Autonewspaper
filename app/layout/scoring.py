"""Layout scoring.

Implements the score of specification §11:

``VisualBalance + ContentHierarchy + Readability + SpaceEfficiency +
ImageQuality + TypographyQuality - OverlapPenalty - OverflowPenalty -
ExcessiveWhitespacePenalty``

Each positive component is normalised to ``0..1`` and multiplied by its
weight, so the raw score is comparable across page sizes and templates.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from app.layout.constraints import ConstraintReport
from app.layout.geometry import alignment_score, center_of_mass, coverage, find_gaps, total_overlap_area
from app.models.schemas import ElementSpec, IssueType, PageLayout
from app.templates.schema import TemplateSpec

log = logging.getLogger(__name__)

WEIGHTS: dict[str, float] = {
    "visual_balance": 18.0,
    "content_hierarchy": 18.0,
    "readability": 16.0,
    "space_efficiency": 16.0,
    "image_quality": 14.0,
    "typography_quality": 18.0,
}

PENALTIES: dict[str, float] = {
    "overlap": 55.0,
    "overflow": 45.0,
    "whitespace": 30.0,
    "constraint": 1.0,
}


@dataclass
class LayoutScore:
    """A scored page."""

    total: float
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)

    def breakdown(self) -> dict[str, float]:
        """Flat mapping used by the UI and stored on the page."""
        out = {k: round(v, 2) for k, v in self.components.items()}
        out.update({f"penalty_{k}": round(-v, 2) for k, v in self.penalties.items()})
        out["total"] = round(self.total, 2)
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"total": round(self.total, 2), "components": self.breakdown()}


class LayoutScorer:
    """Computes the quality score of a candidate page."""

    def __init__(self, template: TemplateSpec) -> None:
        self.template = template
        self.rules = template.layout_rules

    def score(
        self,
        page: PageLayout,
        report: ConstraintReport | None = None,
        *,
        asset_quality: dict[int, float] | None = None,
    ) -> LayoutScore:
        """Score *page*; *report* avoids re-running the constraint checker."""
        elements = page.elements
        if not elements:
            return LayoutScore(total=0.0, components={k: 0.0 for k in WEIGHTS})

        components = {
            "visual_balance": self._visual_balance(page),
            "content_hierarchy": self._hierarchy(elements),
            "readability": self._readability(page),
            "space_efficiency": self._space_efficiency(page),
            "image_quality": self._image_quality(elements, asset_quality or {}),
            "typography_quality": self._typography_quality(elements),
        }
        weighted = {key: value * WEIGHTS[key] for key, value in components.items()}

        penalties = {
            "overlap": self._overlap_penalty(page),
            "overflow": self._overflow_penalty(elements),
            "whitespace": self._whitespace_penalty(page),
            "constraint": (report.penalty() * 0.35) if report else 0.0,
        }
        total = sum(weighted.values()) - sum(penalties.values())
        return LayoutScore(
            total=round(max(0.0, min(100.0, total)), 2),
            components=weighted,
            penalties=penalties,
        )

    def score_plan(self, pages: list[PageLayout]) -> float:
        """Mean score of the pages that actually carry content.

        Pages left empty because the edition ran out of copy are reported
        separately by QA; averaging their zero score would hide the quality of
        the pages that were composed.
        """
        scored = [p for p in pages if not p.meta.get("empty") and p.elements]
        if not scored:
            return 0.0
        return round(sum(p.score for p in scored) / len(scored), 2)

    # ---------------------------------------------------------- components
    def _visual_balance(self, page: PageLayout) -> float:
        """How close the optical centre of mass is to the page centre."""
        content = page.content_rect
        rects = [e.rect for e in page.elements if e.type.value not in ("folio", "rule")]
        if not rects:
            return 0.0
        cx, cy = center_of_mass(rects)
        target_x, target_y = content.center
        # A slightly high centre of mass reads better than a low one.
        target_y -= content.height * 0.04
        dx = abs(cx - target_x) / max(1e-6, content.width / 2)
        dy = abs(cy - target_y) / max(1e-6, content.height / 2)
        balance = 1.0 - min(1.0, math.hypot(dx, dy) / 1.4142)
        return 0.65 * balance + 0.35 * alignment_score(rects)

    def _hierarchy(self, elements: list[ElementSpec]) -> float:
        """Reward a clear dominant story and a readable size ladder."""
        headlines = [e for e in elements if e.type.value == "headline" and e.typography]
        if not headlines:
            return 0.35
        sizes = sorted((e.typography.size_pt for e in headlines), reverse=True)  # type: ignore[union-attr]
        if len(sizes) == 1:
            ladder = 0.85
        else:
            # A step of roughly 1.2x-1.9x between consecutive headline sizes is
            # the range newspaper display type actually uses; anything inside it
            # reads as a deliberate ladder, and the score tapers outside it.
            low, high = 1.2, 1.9
            ratios = [sizes[i] / max(1e-6, sizes[i + 1]) for i in range(len(sizes) - 1)]
            steps = []
            for ratio in ratios:
                if low <= ratio <= high:
                    steps.append(1.0)
                elif ratio < low:
                    steps.append(max(0.0, 1.0 - (low - ratio) / (low - 1.0)))
                else:
                    steps.append(max(0.0, 1.0 - (ratio - high) / high))
            ladder = sum(steps) / len(steps)

        areas = sorted((e.rect.area for e in elements if e.article_id is not None), reverse=True)
        if len(areas) >= 2 and sum(areas) > 0:
            dominance = areas[0] / sum(areas)
            # A lead occupying 30-55% of the editorial area is the newspaper norm.
            dominance_score = 1.0 - min(1.0, abs(dominance - 0.42) / 0.42)
        else:
            dominance_score = 0.8
        return 0.55 * ladder + 0.45 * dominance_score

    def _readability(self, page: PageLayout) -> float:
        """Measure size, measure width and line length of the body text."""
        bodies = [e for e in page.elements if e.type.value == "body" and e.typography]
        if not bodies:
            return 0.5
        scores: list[float] = []
        for element in bodies:
            typography = element.typography
            assert typography is not None
            size_score = min(1.0, max(0.0, (typography.size_pt - self.rules.min_body_size_pt) / 2.5 + 0.55))
            column_width = element.rect.width / max(1, typography.columns)
            # 42-60 mm is a comfortable measure for newspaper body text.
            if column_width < 30:
                measure = column_width / 30.0 * 0.7
            elif column_width > 78:
                measure = max(0.25, 1.0 - (column_width - 78) / 60.0)
            else:
                measure = 1.0 - abs(column_width - 51.0) / 60.0
            leading_ratio = typography.leading_pt / max(1e-6, typography.size_pt)
            leading = 1.0 - min(1.0, abs(leading_ratio - 1.38) / 0.5)
            scores.append(0.4 * size_score + 0.35 * max(0.0, measure) + 0.25 * max(0.0, leading))
        return sum(scores) / len(scores)

    def _space_efficiency(self, page: PageLayout) -> float:
        """Reward coverage close to the template's white-space target."""
        content = page.content_rect
        covered = coverage([e.rect for e in page.elements], content)
        target = 1.0 - self.rules.whitespace_target
        return max(0.0, 1.0 - abs(covered - target) / max(1e-6, target))

    def _image_quality(self, elements: list[ElementSpec], asset_quality: dict[int, float]) -> float:
        """Combine picture presence, aspect sanity and measured asset quality."""
        images = [e for e in elements if e.is_image]
        if not images:
            return 0.45 if any(e.article_id for e in elements) else 0.6
        scores: list[float] = []
        for element in images:
            aspect = element.rect.aspect
            aspect_score = 1.0 if 0.5 <= aspect <= 2.6 else max(0.2, 1.0 - abs(aspect - 1.5) / 3.0)
            quality = asset_quality.get(element.asset_id or -1)
            quality_score = (quality / 100.0) if quality is not None else 0.65
            presence = 1.0 if element.image_path else 0.15
            scores.append(0.35 * aspect_score + 0.4 * quality_score + 0.25 * presence)
        density = min(1.0, len(images) / max(1.0, self.rules.max_images_per_page))
        return 0.85 * (sum(scores) / len(scores)) + 0.15 * density

    def _typography_quality(self, elements: list[ElementSpec]) -> float:
        """Reward style consistency, direction correctness and no min-size hacks."""
        text_elements = [e for e in elements if e.is_text and e.typography]
        if not text_elements:
            return 0.4
        expected = self.template.direction
        families = {e.typography.font_family for e in text_elements}  # type: ignore[union-attr]
        family_score = 1.0 if len(families) <= 3 else max(0.3, 1.0 - (len(families) - 3) * 0.2)
        direction_ok = sum(
            1
            for e in text_elements
            if e.typography.direction == expected  # type: ignore[union-attr]
        ) / len(text_elements)
        squeezed = sum(1 for e in text_elements if e.meta.get("used_min_size")) / len(text_elements)
        truncated = sum(1 for e in text_elements if e.meta.get("truncated")) / len(text_elements)
        return max(
            0.0,
            0.3 * family_score + 0.35 * direction_ok + 0.2 * (1.0 - squeezed) + 0.15 * (1.0 - truncated),
        )

    # ----------------------------------------------------------- penalties
    def _overlap_penalty(self, page: PageLayout) -> float:
        rects = [e.rect for e in page.elements]
        area = total_overlap_area(rects, tolerance=0.4)
        if area <= 0:
            return 0.0
        ratio = area / max(1e-6, page.content_rect.area)
        return min(PENALTIES["overlap"], PENALTIES["overlap"] * min(1.0, ratio * 12.0))

    def _overflow_penalty(self, elements: list[ElementSpec]) -> float:
        overflow = sum(e.estimated_overflow for e in elements)
        if overflow <= 0:
            return 0.0
        return min(PENALTIES["overflow"], PENALTIES["overflow"] * min(1.0, overflow * 2.2))

    def _whitespace_penalty(self, page: PageLayout) -> float:
        gaps = find_gaps([e.rect for e in page.elements], page.content_rect, min_ratio=0.08)
        if not gaps:
            return 0.0
        worst = gaps[0].ratio
        if worst <= self.rules.whitespace_target:
            return 0.0
        excess = (worst - self.rules.whitespace_target) / max(1e-6, 1.0 - self.rules.whitespace_target)
        return min(PENALTIES["whitespace"], PENALTIES["whitespace"] * min(1.0, excess * 1.8))

    # ---------------------------------------------------------- reporting
    def issues_from_score(self, score: LayoutScore) -> list[tuple[IssueType, float]]:
        """Map non-zero penalties to the issue types the QA stage reports."""
        mapping = {
            "overlap": IssueType.OVERLAP,
            "overflow": IssueType.TEXT_OVERFLOW,
            "whitespace": IssueType.EXCESSIVE_WHITESPACE,
        }
        return [
            (mapping[key], value) for key, value in score.penalties.items() if key in mapping and value > 0.5
        ]
