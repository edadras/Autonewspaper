"""Vision QA and the autonomous correction loop.

Implements specification §15 and §16: after a page is built it is rendered,
analysed from three independent angles (geometry, pixels, and the vision model
when one is configured), scored, and - while the score is below the threshold
and iterations remain - corrected and rebuilt. The best result is always kept,
even when the threshold is never reached.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ai.registry import AIService
from app.core.errors import VisionQAError
from app.layout.engine import LayoutEngine
from app.models.schemas import (
    EditionQAReport,
    IssueType,
    PageLayout,
    QAIssue,
    QAReport,
    Rect,
    Severity,
)
from app.templates.schema import TemplateSpec
from app.vision.analyzer import PageAnalyzer, score_from_issues
from app.vision.corrector import CorrectionAction, CorrectionResult, LayoutCorrector

log = logging.getLogger(__name__)

#: Signature of the callable that produces a page image for QA.
#: It receives the page and returns ``(preview_path, indesign_report | None)``.
RenderFn = Callable[[PageLayout], tuple[Path, dict[str, Any] | None]]


@dataclass
class IterationRecord:
    """One turn of the correction loop."""

    iteration: int
    score: float
    issues: int
    correction: dict[str, Any] | None = None
    preview: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "iteration": self.iteration,
            "score": round(self.score, 2),
            "issues": self.issues,
            "correction": self.correction,
            "preview": self.preview,
        }


@dataclass
class LoopResult:
    """Outcome of running the correction loop over one page."""

    page: PageLayout
    report: QAReport
    iterations: list[IterationRecord] = field(default_factory=list)
    passed: bool = False
    stopped_because: str = ""
    """Why the loop ended early, when it did - a timeout or a failing renderer."""

    @property
    def best_score(self) -> float:
        """Score of the page that was kept."""
        return self.report.score

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "page": self.page.index,
            "score": round(self.report.score, 2),
            "passed": self.passed,
            "issues": [issue.model_dump(mode="json") for issue in self.report.issues],
            "iterations": [record.to_dict() for record in self.iterations],
            "stopped_because": self.stopped_because,
        }


class VisionQAAgent:
    """Analyses rendered pages and drives the correction loop."""

    def __init__(
        self,
        engine: LayoutEngine,
        template: TemplateSpec,
        *,
        ai: AIService | None = None,
        threshold: float = 90.0,
        max_iterations: int = 5,
        timeout_seconds: float = 600.0,
        max_retries: int = 3,
        use_vision_model: bool = True,
        preview_dpi: int = 110,
    ) -> None:
        self.engine = engine
        self.template = template
        self.ai = ai
        self.threshold = threshold
        self.max_iterations = max(1, max_iterations)
        self.timeout_seconds = max(10.0, timeout_seconds)
        self.max_retries = max(0, max_retries)
        self.use_vision_model = use_vision_model
        self.analyzer = PageAnalyzer(preview_dpi)
        self.corrector = LayoutCorrector(engine)

    # ------------------------------------------------------------- analysis
    def analyze_page(
        self,
        page: PageLayout,
        *,
        preview_path: Path | None = None,
        indesign_report: dict[str, Any] | None = None,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
        iteration: int = 0,
    ) -> QAReport:
        """Produce the QA report for one page."""
        analyzed_by: list[str] = ["geometry"]
        score, constraint_report = self.engine.rescore(
            page, asset_quality=asset_quality, asset_pixels=asset_pixels
        )
        issues = [self._issue_from_violation(page, v) for v in constraint_report.violations]
        metrics: dict[str, float] = dict(constraint_report.metrics)

        if preview_path and Path(preview_path).exists():
            pixel_metrics = self.analyzer.analyze_pixels(preview_path, page)
            pixel_issues = self.analyzer.issues_from_pixels(
                pixel_metrics,
                page,
                whitespace_target=self.template.layout_rules.whitespace_target,
            )
            issues.extend(pixel_issues)
            metrics.update(pixel_metrics.to_dict())
            analyzed_by.append("pixels")

        if indesign_report:
            issues.extend(self.analyzer.issues_from_indesign(indesign_report, page))
            analyzed_by.append("indesign")

        vision_score: float | None = None
        if self.use_vision_model and self.ai is not None and preview_path and Path(preview_path).exists():
            vision_issues, vision_score = self._vision_review(page, Path(preview_path), metrics)
            issues.extend(vision_issues)
            if vision_issues or vision_score is not None:
                analyzed_by.append("vision_ai")

        issues = self._deduplicate(issues)
        final = score_from_issues(score.total, issues)
        if vision_score is not None:
            # The model is one opinion among three; it is averaged in with a
            # low weight and can never raise a page above its measured score.
            final = round(min(final, 0.75 * final + 0.25 * vision_score), 2)

        page.qa_score = final
        page.meta["qa"] = {"score": final, "issues": len(issues), "analyzed_by": analyzed_by}
        return QAReport(
            page_index=page.index,
            score=final,
            issues=issues,
            preview_path=str(preview_path) if preview_path else None,
            metrics=metrics,
            analyzed_by=analyzed_by,
            iteration=iteration,
        )

    def _vision_review(
        self, page: PageLayout, preview: Path, metrics: dict[str, float]
    ) -> tuple[list[QAIssue], float | None]:
        """Ask the configured vision model for a second opinion."""
        assert self.ai is not None
        context = {
            "page_index": page.index,
            "product_type": self.template.product_type,
            "language": self.template.language,
            "direction": self.template.direction,
            "page_width_mm": round(page.width_mm, 1),
            "page_height_mm": round(page.height_mm, 1),
            "columns": page.columns,
            "measurements_json": _measurements(page, metrics),
        }
        try:
            payload = self.ai.review_page(preview, context)
        except Exception as exc:  # noqa: BLE001 - QA must never abort a run
            log.warning("Vision review of page %d failed: %s", page.index, exc)
            return ([], None)
        if payload.get("unavailable"):
            return ([], None)

        issues: list[QAIssue] = []
        for item in payload.get("issues") or []:
            if not isinstance(item, dict):
                continue
            try:
                issue_type = IssueType(str(item.get("type", "")))
            except ValueError:
                continue
            severity_value = str(item.get("severity", "medium")).lower()
            severity = (
                Severity(severity_value) if severity_value in {s.value for s in Severity} else Severity.MEDIUM
            )
            element_id = str(item.get("element_id") or "") or None
            if element_id and page.element(element_id) is None:
                element_id = None
            issues.append(
                QAIssue(
                    type=issue_type,
                    severity=severity,
                    element_id=element_id,
                    page_index=page.index,
                    message=str(item.get("message", ""))[:300],
                    suggestion=str(item.get("suggestion", ""))[:300],
                    detected_by="vision_ai",
                )
            )
        raw_score = payload.get("score")
        try:
            score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            score = None
        return (issues, score)

    @staticmethod
    def _issue_from_violation(page: PageLayout, violation: Any) -> QAIssue:
        """Convert a constraint violation into a QA issue."""
        return QAIssue(
            type=violation.type,
            severity=violation.severity,
            element_id=violation.element_id,
            page_index=page.index,
            message=violation.message,
            rect=violation.rect,
            suggestion=violation.suggestion,
            detected_by="geometry",
        )

    @staticmethod
    def _deduplicate(issues: list[QAIssue]) -> list[QAIssue]:
        """Collapse issues that different detectors reported about one frame.

        The measured detectors (geometry, pixels, InDesign) win over the vision
        model, so a model comment never replaces a measurement.
        """
        rank = {"indesign": 0, "geometry": 1, "pixels": 2, "vision_ai": 3}
        best: dict[tuple[str, str | None], QAIssue] = {}
        for issue in issues:
            key = (issue.type.value, issue.element_id)
            current = best.get(key)
            if current is None or rank[issue.detected_by] < rank[current.detected_by]:
                best[key] = issue
            elif rank[issue.detected_by] == rank[current.detected_by] and issue.weight > current.weight:
                best[key] = issue
        return sorted(best.values(), key=lambda i: (-i.weight, i.type.value))

    # ------------------------------------------------------------ corrections
    def correct_page(
        self,
        page: PageLayout,
        report: QAReport,
        *,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
        iteration: int = 0,
    ) -> CorrectionResult:
        """Choose and apply corrections for a page that failed QA."""
        actions = self.corrector.heuristic_actions(page, report.issues)

        if not actions and report.score < self.threshold and page.meta.get("blocks"):
            # Nothing is broken, the page is simply not good enough; the only
            # lever left is recomposing it with a different strategy.
            actions.append(
                CorrectionAction(
                    "rebuild_page",
                    None,
                    {"strategy": self.corrector._next_strategy(page)},
                    "Score below threshold with no specific defect; try another composition",
                )
            )

        if self.ai is not None and self.engine.template.layout_rules.allowed_strategies:
            try:
                payload = self.ai.correction_plan(
                    {
                        "page_index": page.index,
                        "score": round(report.score, 1),
                        "threshold": self.threshold,
                        "iteration": iteration,
                        "max_iterations": self.max_iterations,
                        "issues_json": _issues_json(report.issues),
                        "elements_json": _elements_json(page),
                    }
                )
                proposed = self.corrector.parse_actions(payload)
                if proposed:
                    known = {(a.action, a.element_id) for a in actions}
                    actions.extend(a for a in proposed if (a.action, a.element_id) not in known)
            except Exception as exc:  # noqa: BLE001
                log.warning("AI correction planning failed on page %d: %s", page.index, exc)

        return self.corrector.apply(page, actions, asset_quality=asset_quality, asset_pixels=asset_pixels)

    # ------------------------------------------------------------------ loop
    def run_loop(
        self,
        page: PageLayout,
        render: RenderFn,
        *,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
        on_iteration: Callable[[IterationRecord], None] | None = None,
    ) -> LoopResult:
        """Render, analyse and correct until the page passes or attempts run out."""
        best_page = page.model_copy(deep=True)
        best_report: QAReport | None = None
        records: list[IterationRecord] = []
        started = time.monotonic()
        render_failures = 0
        stopped = ""

        for iteration in range(1, self.max_iterations + 1):
            # §57: iterations are not the only bound. A page rendered through
            # InDesign can take minutes, so the loop also watches the clock and
            # gives up on a renderer that keeps failing, keeping the best result
            # it has instead of retrying forever.
            elapsed = time.monotonic() - started
            if iteration > 1 and elapsed > self.timeout_seconds:
                stopped = f"timed out after {elapsed:.0f}s"
                log.warning("Page %d QA %s; keeping the best result", page.index, stopped)
                break
            try:
                preview, indesign_report = render(page)
                render_failures = 0
            except Exception as exc:  # noqa: BLE001 - QA must degrade, not crash
                render_failures += 1
                log.error("Rendering page %d failed on iteration %d: %s", page.index, iteration, exc)
                preview, indesign_report = None, None  # type: ignore[assignment]
                if render_failures > self.max_retries:
                    stopped = f"rendering failed {render_failures} times in a row"
                    log.error("Page %d QA abandoned: %s", page.index, stopped)
                    if best_report is not None:
                        break

            report = self.analyze_page(
                page,
                preview_path=Path(preview) if preview else None,
                indesign_report=indesign_report,
                asset_quality=asset_quality,
                asset_pixels=asset_pixels,
                iteration=iteration,
            )
            record = IterationRecord(
                iteration=iteration,
                score=report.score,
                issues=len(report.issues),
                preview=str(preview) if preview else None,
            )

            if best_report is None or report.score > best_report.score:
                best_report = report
                best_page = page.model_copy(deep=True)

            if report.passed(self.threshold):
                records.append(record)
                if on_iteration:
                    on_iteration(record)
                log.info(
                    "Page %d passed QA on iteration %d with score %.1f",
                    page.index,
                    iteration,
                    report.score,
                )
                page.qa_score = report.score
                page.iterations = iteration
                return LoopResult(page=page, report=report, iterations=records, passed=True)

            if iteration == self.max_iterations:
                records.append(record)
                if on_iteration:
                    on_iteration(record)
                break

            correction = self.correct_page(
                page,
                report,
                asset_quality=asset_quality,
                asset_pixels=asset_pixels,
                iteration=iteration,
            )
            record.correction = correction.to_dict()
            records.append(record)
            if on_iteration:
                on_iteration(record)

            if not correction.applied:
                log.info(
                    "Page %d: no correction improved the score (%.1f); keeping the best result",
                    page.index,
                    report.score,
                )
                break

        if best_report is None:
            # Every iteration failed to render and nothing was ever analysed.
            raise VisionQAError(
                f"Page {page.index} could not be analysed: {stopped or 'no iteration produced a report'}",
                context={"page": page.index, "iterations": len(records)},
            )
        log.warning(
            "Page %d did not reach the QA threshold (%.1f < %.1f) after %d iteration(s)%s; "
            "keeping the best result",
            page.index,
            best_report.score,
            self.threshold,
            len(records),
            f" ({stopped})" if stopped else "",
        )
        best_page.qa_score = best_report.score
        best_page.iterations = len(records)
        return LoopResult(
            page=best_page, report=best_report, iterations=records, passed=False, stopped_because=stopped
        )

    def edition_report(self, reports: list[QAReport], project_id: int, iteration: int = 0) -> EditionQAReport:
        """Aggregate per-page reports into an edition report."""
        return EditionQAReport(project_id=project_id, pages=reports, iteration=iteration)


def _measurements(page: PageLayout, metrics: dict[str, float]) -> str:
    """Compact JSON of the measured geometry handed to the vision model."""
    import json

    payload = {
        "metrics": {k: round(v, 4) for k, v in metrics.items()},
        "elements": [
            {
                "id": element.id,
                "type": element.type.value,
                "x": round(element.rect.x, 1),
                "y": round(element.rect.y, 1),
                "w": round(element.rect.width, 1),
                "h": round(element.rect.height, 1),
                "pt": round(element.typography.size_pt, 1) if element.typography else None,
            }
            for element in page.elements
            if not element.locked
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _issues_json(issues: list[QAIssue]) -> str:
    """Compact JSON of the issues handed to the correction prompt."""
    import json

    return json.dumps(
        [
            {
                "type": issue.type.value,
                "severity": issue.severity.value,
                "element_id": issue.element_id,
                "message": issue.message,
            }
            for issue in issues
        ],
        ensure_ascii=False,
    )


def _elements_json(page: PageLayout) -> str:
    """Compact JSON of the page's frames handed to the correction prompt."""
    import json

    return json.dumps(
        [
            {
                "id": element.id,
                "type": element.type.value,
                "rect": [round(v, 1) for v in element.rect.to_tuple()],
                "overflow": round(element.estimated_overflow, 3),
                "words": len(element.text.split()) if element.is_text else 0,
            }
            for element in page.elements
            if not element.locked
        ],
        ensure_ascii=False,
    )


def empty_rect() -> Rect:
    """A zero rectangle (used as a neutral default)."""
    return Rect(x=0, y=0, width=0, height=0)
