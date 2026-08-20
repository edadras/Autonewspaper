"""Constraint checking.

The layout engine is constraint-based: a candidate is only a valid page if it
satisfies the hard constraints of specification §10. This module evaluates
them and returns structured violations that both the scorer (as penalties)
and the QA stage (as issues) consume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.layout.geometry import find_gaps, total_overlap_area
from app.models.schemas import ElementSpec, IssueType, PageLayout, Rect, Severity
from app.templates.schema import TemplateSpec
from app.utils.units import effective_dpi

log = logging.getLogger(__name__)


@dataclass
class Violation:
    """A broken constraint."""

    type: IssueType
    severity: Severity
    message: str
    element_id: str | None = None
    rect: Rect | None = None
    magnitude: float = 0.0
    """How badly the constraint is broken, normalised where meaningful."""
    suggestion: str = ""

    @property
    def hard(self) -> bool:
        """Whether this violation makes the page unusable as-is."""
        return self.severity in (Severity.HIGH, Severity.CRITICAL)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "message": self.message,
            "element_id": self.element_id,
            "magnitude": round(self.magnitude, 4),
            "suggestion": self.suggestion,
            "rect": self.rect.to_tuple() if self.rect else None,
        }


@dataclass
class ConstraintReport:
    """Result of checking one page."""

    page_index: int
    violations: list[Violation] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the page has no hard violation."""
        return not any(v.hard for v in self.violations)

    def of_type(self, issue: IssueType) -> list[Violation]:
        """All violations of a given type."""
        return [v for v in self.violations if v.type is issue]

    def penalty(self) -> float:
        """Total weighted penalty used by the scorer."""
        weights = {
            Severity.LOW: 1.0,
            Severity.MEDIUM: 3.5,
            Severity.HIGH: 9.0,
            Severity.CRITICAL: 18.0,
        }
        return sum(weights[v.severity] * (1.0 + min(2.0, v.magnitude)) for v in self.violations)


class ConstraintChecker:
    """Evaluates the hard and soft constraints of a page."""

    def __init__(self, template: TemplateSpec, *, overlap_tolerance: float = 0.4) -> None:
        self.template = template
        self.rules = template.layout_rules
        self.overlap_tolerance = overlap_tolerance

    def check(self, page: PageLayout, *, asset_pixels: dict[int, tuple[int, int]] | None = None) -> ConstraintReport:
        """Run every constraint against *page*."""
        report = ConstraintReport(page_index=page.index)
        content = page.content_rect
        elements = page.elements
        asset_pixels = asset_pixels or {}

        self._check_bounds(elements, page, content, report)
        self._check_overlap(elements, report)
        self._check_sizes(elements, report)
        self._check_typography(elements, report)
        self._check_images(elements, asset_pixels, report)
        self._check_whitespace(elements, content, report)
        self._check_hierarchy(elements, report)
        self._check_alignment(elements, page, report)
        self._check_direction(elements, report)

        rects = [e.rect for e in elements]
        report.metrics.update(
            {
                "element_count": float(len(elements)),
                "overlap_area_mm2": round(total_overlap_area(rects, self.overlap_tolerance), 2),
                "ink_area_mm2": round(sum(r.area for r in rects), 2),
                "content_area_mm2": round(content.area, 2),
                "violations": float(len(report.violations)),
            }
        )
        return report

    # ------------------------------------------------------------ sections
    def _check_bounds(
        self, elements: list[ElementSpec], page: PageLayout, content: Rect, report: ConstraintReport
    ) -> None:
        trim = page.page_rect
        for element in elements:
            rect = element.rect
            if not trim.contains(rect, tolerance=0.5):
                overshoot = max(
                    trim.x - rect.x, rect.right - trim.right, trim.y - rect.y, rect.bottom - trim.bottom
                )
                report.violations.append(
                    Violation(
                        type=IssueType.OUT_OF_BOUNDS,
                        severity=Severity.CRITICAL,
                        message=f"'{element.frame_name}' extends {overshoot:.1f} mm outside the page",
                        element_id=element.id,
                        rect=rect,
                        magnitude=overshoot / max(1.0, min(trim.width, trim.height)),
                        suggestion="Move the frame back inside the trim box.",
                    )
                )
                continue
            # Full-bleed images and master-page furniture (folios, running
            # heads) legitimately sit in the margin; running text may not.
            if element.is_text and not element.locked and not content.contains(rect, tolerance=0.6):
                intrusion = max(
                    content.x - rect.x, rect.right - content.right,
                    content.y - rect.y, rect.bottom - content.bottom,
                )
                report.violations.append(
                    Violation(
                        type=IssueType.MARGIN_VIOLATION,
                        severity=Severity.HIGH,
                        message=f"'{element.frame_name}' intrudes {intrusion:.1f} mm into the margin",
                        element_id=element.id,
                        rect=rect,
                        magnitude=intrusion / max(1.0, self.template.margins.max()),
                        suggestion="Shrink or move the frame into the live area.",
                    )
                )

    def _check_overlap(self, elements: list[ElementSpec], report: ConstraintReport) -> None:
        for index, first in enumerate(elements):
            for second in elements[index + 1 :]:
                if first.type.value == "rule" or second.type.value == "rule":
                    continue
                intersection = first.rect.intersection(second.rect)
                if intersection.width <= self.overlap_tolerance or intersection.height <= self.overlap_tolerance:
                    continue
                smaller = min(first.rect.area, second.rect.area) or 1.0
                ratio = intersection.area / smaller
                report.violations.append(
                    Violation(
                        type=IssueType.OVERLAP,
                        severity=Severity.CRITICAL if ratio > 0.08 else Severity.HIGH,
                        message=(
                            f"'{first.frame_name}' overlaps '{second.frame_name}' "
                            f"by {intersection.area:.0f} mm² ({ratio * 100:.0f}%)"
                        ),
                        element_id=first.id,
                        rect=intersection,
                        magnitude=ratio,
                        suggestion="Shrink one frame or move it to the next module.",
                    )
                )

    def _check_sizes(self, elements: list[ElementSpec], report: ConstraintReport) -> None:
        """Frames must be large enough to hold at least one line of their type."""
        for element in elements:
            if element.type.value == "rule":
                continue
            min_width = self.rules.min_element_width_mm
            if element.type.value in ("folio", "kicker", "byline", "caption"):
                min_width = min(min_width, 12.0)
            if element.rect.width < min_width:
                report.violations.append(
                    Violation(
                        type=IssueType.OUT_OF_BOUNDS,
                        severity=Severity.MEDIUM,
                        message=f"'{element.frame_name}' is only {element.rect.width:.1f} mm wide",
                        element_id=element.id,
                        magnitude=1.0 - element.rect.width / max(1e-6, min_width),
                        suggestion="Give the frame at least one full column.",
                    )
                )
            min_height = self._min_height(element)
            # Frames are allocated the height the typography engine asked for;
            # allow a sub-millimetre rounding tolerance before flagging them.
            if element.rect.height < min_height - 0.6:
                report.violations.append(
                    Violation(
                        type=IssueType.OUT_OF_BOUNDS,
                        severity=Severity.MEDIUM,
                        message=(
                            f"'{element.frame_name}' is {element.rect.height:.1f} mm high, "
                            f"under the {min_height:.1f} mm needed for one line"
                        ),
                        element_id=element.id,
                        magnitude=1.0 - element.rect.height / max(1e-6, min_height),
                        suggestion="Increase the frame height or drop the element.",
                    )
                )

    def _min_height(self, element: ElementSpec) -> float:
        """Smallest sensible height for a frame.

        Single-line furniture (kickers, bylines, captions, folios) only needs
        its own leading, so the generic minimum would flag perfectly good
        frames; picture and body frames keep the template minimum.
        """
        if element.is_text and element.typography is not None:
            line_mm = element.typography.leading_pt * 25.4 / 72.0
            if element.type.value in ("kicker", "byline", "caption", "folio"):
                return round(line_mm * 1.02, 2)
            return max(self.rules.min_element_height_mm, round(line_mm * 1.02, 2))
        return self.rules.min_element_height_mm

    def _check_typography(self, elements: list[ElementSpec], report: ConstraintReport) -> None:
        for element in elements:
            typography = element.typography
            if typography is None:
                if element.is_text and element.text:
                    report.violations.append(
                        Violation(
                            type=IssueType.LOW_READABILITY,
                            severity=Severity.MEDIUM,
                            message=f"'{element.frame_name}' has no resolved typography",
                            element_id=element.id,
                            suggestion="Re-run the typography engine for this frame.",
                        )
                    )
                continue
            minimum = (
                self.rules.headline_min_size_pt
                if element.type.value in ("headline", "masthead")
                else self.rules.min_body_size_pt
            )
            if typography.size_pt < minimum - 0.01:
                report.violations.append(
                    Violation(
                        type=IssueType.SMALL_FONT,
                        severity=Severity.HIGH,
                        message=(
                            f"'{element.frame_name}' is set at {typography.size_pt:.1f} pt, "
                            f"below the {minimum:.1f} pt minimum"
                        ),
                        element_id=element.id,
                        magnitude=(minimum - typography.size_pt) / max(1e-6, minimum),
                        suggestion="Give the frame more space or shorten the text.",
                    )
                )
            if element.estimated_overflow > 0.001:
                report.violations.append(
                    Violation(
                        type=IssueType.TEXT_OVERFLOW,
                        severity=Severity.CRITICAL if element.estimated_overflow > 0.15 else Severity.HIGH,
                        message=(
                            f"'{element.frame_name}' overflows by "
                            f"{element.estimated_overflow * 100:.0f}%"
                        ),
                        element_id=element.id,
                        magnitude=element.estimated_overflow,
                        suggestion="Enlarge the frame, reduce the size or trim the copy.",
                    )
                )
            if element.is_text and not element.text.strip() and element.type.value != "rule":
                report.violations.append(
                    Violation(
                        type=IssueType.EMPTY_FRAME,
                        severity=Severity.MEDIUM,
                        message=f"'{element.frame_name}' is empty",
                        element_id=element.id,
                        suggestion="Fill the frame or remove it from the plan.",
                    )
                )

    def _check_images(
        self,
        elements: list[ElementSpec],
        asset_pixels: dict[int, tuple[int, int]],
        report: ConstraintReport,
    ) -> None:
        images = [e for e in elements if e.is_image]
        if len(images) > self.rules.max_images_per_page:
            report.violations.append(
                Violation(
                    type=IssueType.UNBALANCED,
                    severity=Severity.LOW,
                    message=f"{len(images)} images on the page (limit {self.rules.max_images_per_page})",
                    magnitude=(len(images) - self.rules.max_images_per_page) / 4.0,
                    suggestion="Drop the weakest picture.",
                )
            )
        for element in images:
            if not element.image_path:
                report.violations.append(
                    Violation(
                        type=IssueType.IMAGE_MISSING,
                        severity=Severity.HIGH,
                        message=f"'{element.frame_name}' has no image assigned",
                        element_id=element.id,
                        suggestion="Assign an asset or enable AI image generation.",
                    )
                )
                continue
            pixels = asset_pixels.get(element.asset_id or -1)
            if not pixels:
                continue
            dpi = min(
                effective_dpi(pixels[0], element.rect.width),
                effective_dpi(pixels[1], element.rect.height),
            )
            if dpi < self.rules.min_image_dpi:
                report.violations.append(
                    Violation(
                        type=IssueType.LOW_IMAGE_RESOLUTION,
                        severity=Severity.HIGH if dpi < self.rules.min_image_dpi * 0.75 else Severity.MEDIUM,
                        message=(
                            f"'{element.frame_name}' would print at {dpi:.0f} dpi "
                            f"(minimum {self.rules.min_image_dpi:.0f})"
                        ),
                        element_id=element.id,
                        magnitude=1.0 - dpi / max(1e-6, self.rules.min_image_dpi),
                        suggestion="Use a larger source image or reduce the frame size.",
                    )
                )

    def _check_whitespace(
        self, elements: list[ElementSpec], content: Rect, report: ConstraintReport
    ) -> None:
        rects = [e.rect for e in elements]
        for gap in find_gaps(rects, content, min_ratio=0.06):
            if gap.ratio < 0.10:
                continue
            report.violations.append(
                Violation(
                    type=IssueType.EXCESSIVE_WHITESPACE,
                    severity=Severity.HIGH if gap.ratio > 0.22 else Severity.MEDIUM,
                    message=f"{gap.ratio * 100:.0f}% of the live area is an empty block",
                    rect=gap.rect,
                    magnitude=gap.ratio,
                    suggestion="Grow the neighbouring story or pull another item onto the page.",
                )
            )

    def _check_hierarchy(self, elements: list[ElementSpec], report: ConstraintReport) -> None:
        headlines = [
            e for e in elements if e.type.value == "headline" and e.typography is not None
        ]
        if len(headlines) < 2:
            return
        sizes = sorted((e.typography.size_pt for e in headlines), reverse=True)  # type: ignore[union-attr]
        if sizes[0] - sizes[1] < 1.5:
            report.violations.append(
                Violation(
                    type=IssueType.POOR_HIERARCHY,
                    severity=Severity.MEDIUM,
                    message=(
                        f"The two largest headlines are the same weight "
                        f"({sizes[0]:.1f} pt vs {sizes[1]:.1f} pt)"
                    ),
                    magnitude=1.0 - (sizes[0] - sizes[1]) / 1.5,
                    suggestion="Enlarge the lead headline or reduce the second story.",
                )
            )

    def _check_alignment(
        self, elements: list[ElementSpec], page: PageLayout, report: ConstraintReport
    ) -> None:
        column_width = page.column_width()
        unit = column_width + page.gutter_mm
        if unit <= 0:
            return
        for element in elements:
            if element.locked or element.type.value in ("rule", "masthead", "folio"):
                # Master-page furniture is positioned absolutely by the template.
                continue
            offset = (element.rect.x - page.content_rect.x) % unit
            deviation = min(offset, unit - offset)
            if deviation > 1.6:
                report.violations.append(
                    Violation(
                        type=IssueType.MISALIGNMENT,
                        severity=Severity.LOW,
                        message=f"'{element.frame_name}' sits {deviation:.1f} mm off the column grid",
                        element_id=element.id,
                        magnitude=deviation / max(1e-6, unit),
                        suggestion="Snap the frame to the nearest column.",
                    )
                )

    def _check_direction(self, elements: list[ElementSpec], report: ConstraintReport) -> None:
        expected = "rtl" if self.template.direction == "rtl" else "ltr"
        for element in elements:
            typography = element.typography
            if typography is None or not element.is_text:
                continue
            if typography.direction != expected:
                report.violations.append(
                    Violation(
                        type=IssueType.RTL_PROBLEM,
                        severity=Severity.HIGH,
                        message=(
                            f"'{element.frame_name}' is set {typography.direction} "
                            f"but the template is {expected}"
                        ),
                        element_id=element.id,
                        magnitude=1.0,
                        suggestion="Re-resolve the typography for this frame.",
                    )
                )
            elif expected == "rtl" and typography.alignment == "left":
                report.violations.append(
                    Violation(
                        type=IssueType.RTL_PROBLEM,
                        severity=Severity.MEDIUM,
                        message=f"'{element.frame_name}' is left-aligned in a right-to-left page",
                        element_id=element.id,
                        magnitude=0.5,
                        suggestion="Use right or justified alignment.",
                    )
                )
