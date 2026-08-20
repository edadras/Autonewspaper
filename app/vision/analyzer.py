"""Page analysis.

Two independent measurements feed the QA score:

*geometry*
    the constraint checker running over the layout plan (and, when InDesign
    built the document, over the geometry InDesign itself reports);
*pixels*
    what the rendered page actually looks like - ink coverage, empty blocks,
    contrast, whether a frame that should carry text is blank, and whether ink
    strays into the margins.

The vision model, when one is configured, is a third opinion layered on top;
it can add issues but never overrules a measured one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

from app.models.schemas import ElementSpec, IssueType, PageLayout, QAIssue, Rect, Severity
from app.utils.units import mm_to_px

log = logging.getLogger(__name__)

INK_THRESHOLD = 232
"""Pixels darker than this count as ink on a white page."""


@dataclass
class PixelMetrics:
    """What the rendered page measures."""

    ink_coverage: float = 0.0
    """Share of the live area covered by ink."""
    margin_ink: float = 0.0
    """Share of the margin area covered by ink."""
    contrast: float = 0.0
    mean_luminance: float = 0.0
    edge_density: float = 0.0
    empty_blocks: list[Rect] = field(default_factory=list)
    element_ink: dict[str, float] = field(default_factory=dict)
    width_px: int = 0
    height_px: int = 0

    def to_dict(self) -> dict[str, float]:
        """Flat numeric mapping for the QA report."""
        return {
            "ink_coverage": round(self.ink_coverage, 4),
            "margin_ink": round(self.margin_ink, 4),
            "contrast": round(self.contrast, 2),
            "mean_luminance": round(self.mean_luminance, 2),
            "edge_density": round(self.edge_density, 4),
            "empty_block_count": float(len(self.empty_blocks)),
            "largest_empty_block": round(
                max((r.area for r in self.empty_blocks), default=0.0), 2
            ),
        }


class PageAnalyzer:
    """Measures a rendered page image against its plan."""

    def __init__(self, dpi: int = 110) -> None:
        self.dpi = dpi

    # -------------------------------------------------------------- pixels
    def analyze_pixels(self, image_path: Path | str, page: PageLayout) -> PixelMetrics:
        """Measure the rendered page."""
        metrics = PixelMetrics()
        path = Path(image_path)
        if not path.exists():
            log.warning("Cannot analyse missing preview: %s", path)
            return metrics
        with Image.open(path) as source:
            image = source.convert("L")
            metrics.width_px, metrics.height_px = image.size
            scale_x = image.width / max(1e-6, page.width_mm)
            scale_y = image.height / max(1e-6, page.height_mm)

            content = page.content_rect
            content_box = (
                int(content.x * scale_x), int(content.y * scale_y),
                int(content.right * scale_x), int(content.bottom * scale_y),
            )
            live = image.crop(content_box)
            metrics.ink_coverage = self._ink_ratio(live)

            stat = ImageStat.Stat(live)
            metrics.mean_luminance = float(stat.mean[0])
            metrics.contrast = float(stat.stddev[0])
            edges = live.filter(ImageFilter.FIND_EDGES)
            metrics.edge_density = self._ink_ratio(edges, threshold=48, invert=True)

            metrics.margin_ink = self._margin_ink(image, page, scale_x, scale_y)
            metrics.empty_blocks = self._empty_blocks(live, page, content)
            for element in page.elements:
                metrics.element_ink[element.id] = self._element_ink(
                    image, element, scale_x, scale_y
                )
        return metrics

    @staticmethod
    def _ink_ratio(image: Image.Image, threshold: int = INK_THRESHOLD, invert: bool = False) -> float:
        """Share of pixels that carry ink."""
        histogram = image.histogram()
        total = sum(histogram) or 1
        if invert:
            return sum(histogram[threshold:]) / total
        return sum(histogram[:threshold]) / total

    def _margin_ink(
        self, image: Image.Image, page: PageLayout, scale_x: float, scale_y: float
    ) -> float:
        """Ink outside the live area, excluding the folio band."""
        content = page.content_rect
        bands = [
            (0, 0, image.width, int(content.y * scale_y)),
            (0, int(content.bottom * scale_y), image.width, image.height),
            (0, 0, int(content.x * scale_x), image.height),
            (int(content.right * scale_x), 0, image.width, image.height),
        ]
        inked, area = 0.0, 0.0
        for box in bands:
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            band = image.crop(box)
            pixels = band.width * band.height
            inked += self._ink_ratio(band) * pixels
            area += pixels
        return inked / max(1.0, area)

    def _empty_blocks(
        self, live: Image.Image, page: PageLayout, content: Rect
    ) -> list[Rect]:
        """Find blank rectangles in the live area, measured on the pixels."""
        steps_x, steps_y = 20, 26
        cell_w, cell_h = live.width / steps_x, live.height / steps_y
        occupied = [[False] * steps_x for _ in range(steps_y)]
        for row in range(steps_y):
            for col in range(steps_x):
                box = (
                    int(col * cell_w), int(row * cell_h),
                    int((col + 1) * cell_w), int((row + 1) * cell_h),
                )
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                occupied[row][col] = self._ink_ratio(live.crop(box)) > 0.012

        blocks: list[Rect] = []
        seen = [[False] * steps_x for _ in range(steps_y)]
        for row in range(steps_y):
            for col in range(steps_x):
                if occupied[row][col] or seen[row][col]:
                    continue
                width = 0
                while (
                    col + width < steps_x
                    and not occupied[row][col + width]
                    and not seen[row][col + width]
                ):
                    width += 1
                height = 1
                while row + height < steps_y and all(
                    not occupied[row + height][c] and not seen[row + height][c]
                    for c in range(col, col + width)
                ):
                    height += 1
                for r in range(row, row + height):
                    for c in range(col, col + width):
                        seen[r][c] = True
                if width * height < 12:
                    continue
                blocks.append(
                    Rect(
                        x=content.x + col * content.width / steps_x,
                        y=content.y + row * content.height / steps_y,
                        width=width * content.width / steps_x,
                        height=height * content.height / steps_y,
                    )
                )
        return sorted(blocks, key=lambda r: -r.area)

    def _element_ink(
        self, image: Image.Image, element: ElementSpec, scale_x: float, scale_y: float
    ) -> float:
        """Ink coverage inside one element's frame."""
        rect = element.rect
        box = (
            max(0, int(rect.x * scale_x)), max(0, int(rect.y * scale_y)),
            min(image.width, int(rect.right * scale_x)), min(image.height, int(rect.bottom * scale_y)),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return 0.0
        return self._ink_ratio(image.crop(box))

    # -------------------------------------------------------------- issues
    def issues_from_pixels(
        self, metrics: PixelMetrics, page: PageLayout, *, whitespace_target: float = 0.14
    ) -> list[QAIssue]:
        """Turn the pixel measurements into QA issues."""
        issues: list[QAIssue] = []

        if metrics.width_px == 0:
            return issues

        if metrics.margin_ink > 0.02:
            issues.append(
                QAIssue(
                    type=IssueType.MARGIN_VIOLATION,
                    severity=Severity.HIGH if metrics.margin_ink > 0.06 else Severity.MEDIUM,
                    page_index=page.index,
                    message=f"{metrics.margin_ink * 100:.1f}% of the margin area carries ink",
                    suggestion="Pull the frames back inside the live area.",
                    detected_by="pixels",
                )
            )

        content_area = page.content_rect.area or 1.0
        for block in metrics.empty_blocks[:3]:
            ratio = block.area / content_area
            if ratio < max(0.10, whitespace_target):
                continue
            issues.append(
                QAIssue(
                    type=IssueType.EXCESSIVE_WHITESPACE,
                    severity=Severity.HIGH if ratio > 0.24 else Severity.MEDIUM,
                    page_index=page.index,
                    message=f"An empty block covers {ratio * 100:.0f}% of the live area",
                    rect=block,
                    suggestion="Grow a neighbouring story or move another item onto the page.",
                    detected_by="pixels",
                )
            )

        if metrics.ink_coverage < 0.045:
            issues.append(
                QAIssue(
                    type=IssueType.EXCESSIVE_WHITESPACE,
                    severity=Severity.HIGH,
                    page_index=page.index,
                    message=f"The page is nearly blank ({metrics.ink_coverage * 100:.1f}% ink)",
                    suggestion="Check that the text and images were placed.",
                    detected_by="pixels",
                )
            )
        elif metrics.ink_coverage > 0.72:
            issues.append(
                QAIssue(
                    type=IssueType.LOW_READABILITY,
                    severity=Severity.MEDIUM,
                    page_index=page.index,
                    message=f"The page is very dense ({metrics.ink_coverage * 100:.0f}% ink)",
                    suggestion="Trim copy or add white space between stories.",
                    detected_by="pixels",
                )
            )

        if metrics.contrast < 12:
            issues.append(
                QAIssue(
                    type=IssueType.LOW_READABILITY,
                    severity=Severity.MEDIUM,
                    page_index=page.index,
                    message=f"Low tonal contrast on the page ({metrics.contrast:.1f})",
                    suggestion="Check the text colour and image exposure.",
                    detected_by="pixels",
                )
            )

        for element in page.elements:
            ink = metrics.element_ink.get(element.id, 0.0)
            if element.is_text and element.text.strip() and ink < 0.004:
                issues.append(
                    QAIssue(
                        type=IssueType.EMPTY_FRAME,
                        severity=Severity.HIGH,
                        element_id=element.id,
                        page_index=page.index,
                        message=f"'{element.frame_name}' has text but renders blank",
                        rect=element.rect,
                        suggestion="Check the font is installed and the frame is not hidden.",
                        detected_by="pixels",
                    )
                )
            elif element.is_image and ink < 0.02 and element.image_path:
                issues.append(
                    QAIssue(
                        type=IssueType.IMAGE_MISSING,
                        severity=Severity.MEDIUM,
                        element_id=element.id,
                        page_index=page.index,
                        message=f"'{element.frame_name}' looks empty; the picture may not have been placed",
                        rect=element.rect,
                        suggestion="Verify the image link.",
                        detected_by="pixels",
                    )
                )
        return issues

    # ------------------------------------------------------- indesign data
    @staticmethod
    def issues_from_indesign(report: dict[str, Any], page: PageLayout) -> list[QAIssue]:
        """Convert an InDesign page report into QA issues.

        This is the authoritative source when the document exists: InDesign
        reports real overflow and the real effective resolution of every
        placed picture.
        """
        issues: list[QAIssue] = []
        for item in report.get("items", []) or []:
            frame_id = str(item.get("id") or "")
            if item.get("overflows"):
                issues.append(
                    QAIssue(
                        type=IssueType.TEXT_OVERFLOW,
                        severity=Severity.CRITICAL,
                        element_id=frame_id,
                        page_index=page.index,
                        message=f"InDesign reports '{frame_id}' overflows",
                        suggestion="Enlarge the frame, reduce the type size or trim the copy.",
                        detected_by="indesign",
                    )
                )
            ppi = item.get("effective_ppi")
            if ppi and ppi < 200:
                issues.append(
                    QAIssue(
                        type=IssueType.LOW_IMAGE_RESOLUTION,
                        severity=Severity.HIGH if ppi < 150 else Severity.MEDIUM,
                        element_id=frame_id,
                        page_index=page.index,
                        message=f"'{frame_id}' prints at {ppi:.0f} ppi",
                        suggestion="Use a larger source image or a smaller frame.",
                        detected_by="indesign",
                    )
                )
            status = str(item.get("link_status") or "")
            if status and "MISSING" in status.upper():
                issues.append(
                    QAIssue(
                        type=IssueType.IMAGE_MISSING,
                        severity=Severity.CRITICAL,
                        element_id=frame_id,
                        page_index=page.index,
                        message=f"The link for '{frame_id}' is missing",
                        suggestion="Re-place the image; the source file may have moved.",
                        detected_by="indesign",
                    )
                )
        return issues


def score_from_issues(base: float, issues: list[QAIssue]) -> float:
    """Reduce a base score by the weighted penalty of *issues*."""
    penalty = sum(issue.weight for issue in issues)
    return round(max(0.0, min(100.0, base - penalty)), 2)
