"""Layout correction.

Turns QA issues into concrete, validated changes to a page plan. Every action
is checked before it is applied (specification §55: validate -> permit ->
execute -> verify), and the page is re-fitted and re-scored afterwards so a
correction that makes things worse is rolled back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.layout.engine import LayoutEngine
from app.models.schemas import (
    ElementSpec,
    ElementType,
    IssueType,
    PageLayout,
    QAIssue,
    Rect,
    Severity,
)
from app.utils import text as T

log = logging.getLogger(__name__)

#: Allowed actions and the bounds their arguments must respect.
ACTION_LIMITS: dict[str, dict[str, tuple[float, float]]] = {
    "shrink_element": {"factor": (0.75, 0.98)},
    "grow_element": {"factor": (1.02, 1.25)},
    "move_element": {"dx_mm": (-40.0, 40.0), "dy_mm": (-40.0, 40.0)},
    "reduce_text": {"ratio": (0.5, 0.95)},
    "reduce_font": {"delta_pt": (0.25, 1.5)},
    "increase_font": {"delta_pt": (0.25, 2.0)},
    "swap_elements": {},
    "drop_element": {},
    "rebuild_page": {},
}


@dataclass
class CorrectionAction:
    """One requested change."""

    action: str
    element_id: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "action": self.action,
            "element_id": self.element_id,
            "args": self.args,
            "reason": self.reason,
        }


@dataclass
class CorrectionResult:
    """What a correction pass did."""

    applied: list[CorrectionAction] = field(default_factory=list)
    rejected: list[tuple[CorrectionAction, str]] = field(default_factory=list)
    rebuilt: bool = False
    score_before: float = 0.0
    score_after: float = 0.0

    @property
    def improved(self) -> bool:
        """Whether the page scored better afterwards."""
        return self.score_after > self.score_before + 0.01

    @property
    def regressed(self) -> bool:
        """Whether the geometric score got worse."""
        return self.score_after < self.score_before - 0.01

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "applied": [a.to_dict() for a in self.applied],
            "rejected": [{"action": a.to_dict(), "reason": r} for a, r in self.rejected],
            "rebuilt": self.rebuilt,
            "score_before": round(self.score_before, 2),
            "score_after": round(self.score_after, 2),
            "improved": self.improved,
        }


class LayoutCorrector:
    """Validates and applies corrections to a page plan."""

    def __init__(self, engine: LayoutEngine) -> None:
        self.engine = engine

    # -------------------------------------------------------- action source
    def heuristic_actions(self, page: PageLayout, issues: list[QAIssue]) -> list[CorrectionAction]:
        """Derive corrections from the issues without asking a model.

        This is what makes the correction loop work offline and is also the
        baseline the AI proposals are compared against.
        """
        actions: list[CorrectionAction] = []
        seen: set[str] = set()

        for issue in sorted(issues, key=lambda i: -i.weight):
            element = page.element(issue.element_id) if issue.element_id else None
            key = f"{issue.type.value}:{issue.element_id}"
            if key in seen:
                continue
            seen.add(key)

            if issue.type is IssueType.TEXT_OVERFLOW and element is not None:
                overflow = max(0.05, element.estimated_overflow or 0.1)
                if element.type in (ElementType.BODY, ElementType.SIDEBAR):
                    actions.append(
                        CorrectionAction(
                            "reduce_text",
                            element.id,
                            {"ratio": round(max(0.55, 1.0 - overflow * 1.15), 3)},
                            "Trim the copy so the story fits its frame",
                        )
                    )
                else:
                    actions.append(
                        CorrectionAction(
                            "reduce_font",
                            element.id,
                            {"delta_pt": round(min(1.5, 0.5 + overflow * 3), 2)},
                            "Reduce the display size to remove the overflow",
                        )
                    )
            elif issue.type is IssueType.OVERLAP and element is not None:
                actions.append(
                    CorrectionAction(
                        "shrink_element", element.id, {"factor": 0.92},
                        "Shrink the frame to clear the overlap",
                    )
                )
            elif issue.type in (IssueType.MARGIN_VIOLATION, IssueType.OUT_OF_BOUNDS) and element:
                actions.append(
                    CorrectionAction(
                        "move_element", element.id, self._pull_inside(page, element),
                        "Move the frame back into the live area",
                    )
                )
            elif issue.type is IssueType.EXCESSIVE_WHITESPACE:
                target = self._neighbour_of_gap(page, issue.rect)
                if target is not None:
                    if self._under_filled(target):
                        # The frame already covers the space; its copy simply
                        # does not fill it, so set the story larger instead of
                        # making an already page-wide frame bigger.
                        actions.append(
                            CorrectionAction(
                                "increase_font", target.id, {"delta_pt": 1.0},
                                "Set the story larger so it fills its frame",
                            )
                        )
                    else:
                        actions.append(
                            CorrectionAction(
                                "grow_element", target.id, {"factor": 1.12},
                                "Grow the neighbouring story into the empty block",
                            )
                        )
            elif issue.type is IssueType.SMALL_FONT and element is not None:
                actions.append(
                    CorrectionAction(
                        "grow_element", element.id, {"factor": 1.08},
                        "Give the frame room so the type can grow back",
                    )
                )
            elif issue.type is IssueType.POOR_HIERARCHY:
                runner_up = self._second_headline(page)
                if runner_up is not None:
                    actions.append(
                        CorrectionAction(
                            "reduce_font", runner_up.id, {"delta_pt": 1.5},
                            "Hold the second headline below the lead to restore the hierarchy",
                        )
                    )
                lead = self._largest_headline(page)
                if lead is not None:
                    actions.append(
                        CorrectionAction(
                            "grow_element", lead.id, {"factor": 1.06},
                            "Strengthen the lead headline to restore the hierarchy",
                        )
                    )
            elif issue.type is IssueType.EMPTY_FRAME and element is not None:
                actions.append(
                    CorrectionAction("drop_element", element.id, {}, "Remove the empty frame")
                )

        critical = [i for i in issues if i.severity is Severity.CRITICAL]
        if len(critical) >= 3:
            actions.append(
                CorrectionAction(
                    "rebuild_page", None, {"strategy": self._next_strategy(page)},
                    "Too many hard failures; recompose the page with another strategy",
                )
            )
        return actions[:8]

    def parse_actions(self, payload: Any) -> list[CorrectionAction]:
        """Convert a model reply into :class:`CorrectionAction` objects."""
        out: list[CorrectionAction] = []
        items = payload.get("actions") if isinstance(payload, dict) else payload
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("action") or "").strip()
            if name not in ACTION_LIMITS:
                log.warning("Ignoring unknown correction action '%s'", name)
                continue
            out.append(
                CorrectionAction(
                    action=name,
                    element_id=(str(item["element_id"]) if item.get("element_id") else None),
                    args=dict(item.get("args") or {}),
                    reason=str(item.get("reason") or ""),
                )
            )
        return out

    # ------------------------------------------------------------- applying
    def apply(
        self,
        page: PageLayout,
        actions: list[CorrectionAction],
        *,
        asset_quality: dict[int, float] | None = None,
        asset_pixels: dict[int, tuple[int, int]] | None = None,
    ) -> CorrectionResult:
        """Validate and apply *actions*, keeping the page only if it improves."""
        result = CorrectionResult(score_before=page.score)
        snapshot = page.model_copy(deep=True)

        for action in actions:
            error = self._validate(page, action)
            if error:
                result.rejected.append((action, error))
                log.info("Rejected correction %s: %s", action.action, error)
                continue
            try:
                changed = self._execute(page, action)
            except Exception as exc:  # noqa: BLE001 - a bad action must not abort QA
                result.rejected.append((action, f"execution failed: {exc}"))
                log.warning("Correction %s failed: %s", action.action, exc)
                continue
            if changed:
                result.applied.append(action)
                if action.action == "rebuild_page":
                    result.rebuilt = True
            else:
                result.rejected.append((action, "no effect"))

        self.engine.refit(page)
        score, _report = self.engine.rescore(
            page, asset_quality=asset_quality, asset_pixels=asset_pixels
        )
        result.score_after = score.total

        # Only a regression is rolled back. A correction that leaves the
        # geometric score unchanged may still have fixed something only the
        # rendered pixels show (white space, an under-filled frame), and the
        # next iteration of the loop is what judges that.
        if result.applied and result.regressed:
            log.info(
                "Correction pass on page %d made the score worse (%.1f -> %.1f); rolling back",
                page.index, result.score_before, result.score_after,
            )
            page.elements = snapshot.elements
            page.score = snapshot.score
            page.score_breakdown = snapshot.score_breakdown
            page.meta = snapshot.meta
            result.score_after = snapshot.score
            result.rejected.extend((a, "rolled back: score regressed") for a in result.applied)
            result.applied = []
        return result

    # ------------------------------------------------------------ validation
    def _validate(self, page: PageLayout, action: CorrectionAction) -> str | None:
        """Return an error string when *action* must not be applied."""
        if action.action not in ACTION_LIMITS:
            return f"unknown action '{action.action}'"

        needs_element = action.action not in ("rebuild_page",)
        element = page.element(action.element_id) if action.element_id else None
        if needs_element:
            if element is None:
                return f"element '{action.element_id}' does not exist on page {page.index}"
            if element.locked:
                return f"element '{action.element_id}' is locked (master page furniture)"

        for name, (low, high) in ACTION_LIMITS[action.action].items():
            if name not in action.args:
                return f"missing argument '{name}'"
            try:
                value = float(action.args[name])
            except (TypeError, ValueError):
                return f"argument '{name}' is not a number"
            if not (low <= value <= high):
                return f"argument '{name}'={value} is outside the allowed range {low}..{high}"

        if action.action == "swap_elements":
            other = page.element(str(action.args.get("element_id_b", "")))
            if other is None:
                return "the second element of the swap does not exist"
            if other.locked:
                return "the second element is locked"

        if action.action == "rebuild_page":
            strategy = str(action.args.get("strategy", ""))
            allowed = self.engine.template.layout_rules.allowed_strategies
            if strategy and strategy not in allowed:
                return f"strategy '{strategy}' is not allowed by the template"

        if action.action in ("grow_element", "move_element") and element is not None:
            candidate = self._preview(page, element, action)
            if candidate is not None:
                if not page.page_rect.contains(candidate, tolerance=0.5):
                    return "the change would push the frame off the page"
                for other in page.elements:
                    if other.id == element.id or other.type is ElementType.RULE:
                        continue
                    if candidate.overlaps(other.rect, tolerance=0.6):
                        return f"the change would overlap '{other.frame_name}'"
        return None

    def _preview(
        self, page: PageLayout, element: ElementSpec, action: CorrectionAction
    ) -> Rect | None:
        """Geometry the action would produce, for validation."""
        rect = element.rect
        if action.action == "grow_element":
            factor = float(action.args["factor"])
            return Rect(
                x=rect.x, y=rect.y, width=rect.width * factor, height=rect.height * factor
            )
        if action.action == "move_element":
            return Rect(
                x=rect.x + float(action.args["dx_mm"]),
                y=rect.y + float(action.args["dy_mm"]),
                width=rect.width,
                height=rect.height,
            )
        return None

    # ------------------------------------------------------------ execution
    def _execute(self, page: PageLayout, action: CorrectionAction) -> bool:
        """Apply a validated action; returns whether anything changed."""
        element = page.element(action.element_id) if action.element_id else None

        if action.action == "shrink_element" and element:
            factor = float(action.args["factor"])
            element.rect = Rect(
                x=element.rect.x, y=element.rect.y,
                width=element.rect.width * factor, height=element.rect.height * factor,
            )
            return True

        if action.action == "grow_element" and element:
            factor = float(action.args["factor"])
            element.rect = Rect(
                x=element.rect.x, y=element.rect.y,
                width=element.rect.width * factor, height=element.rect.height * factor,
            )
            return True

        if action.action == "move_element" and element:
            element.rect = Rect(
                x=element.rect.x + float(action.args["dx_mm"]),
                y=element.rect.y + float(action.args["dy_mm"]),
                width=element.rect.width, height=element.rect.height,
            )
            return True

        if action.action == "reduce_text" and element:
            ratio = float(action.args["ratio"])
            words = element.text.split()
            keep = max(8, int(len(words) * ratio))
            if keep >= len(words):
                return False
            element.text = " ".join(words[:keep])
            element.meta["truncated"] = True
            element.meta["trimmed_words"] = len(words) - keep
            return True

        if action.action == "reduce_font" and element and element.typography:
            delta = float(action.args["delta_pt"])
            minimum = self.engine.typography.style_spec(element.type).min_size_pt
            new_size = max(minimum, element.typography.size_pt - delta)
            if abs(new_size - element.typography.size_pt) < 0.05:
                return False
            ratio = element.typography.leading_pt / max(1e-6, element.typography.size_pt)
            element.typography.size_pt = round(new_size, 2)
            element.typography.leading_pt = round(new_size * ratio, 2)
            element.meta["used_min_size"] = abs(new_size - minimum) < 0.01
            return True

        if action.action == "increase_font" and element and element.typography:
            delta = float(action.args["delta_pt"])
            maximum = self.engine.typography.style_spec(element.type).max_size_pt
            new_size = min(maximum, element.typography.size_pt + delta)
            if abs(new_size - element.typography.size_pt) < 0.05:
                return False
            ratio = element.typography.leading_pt / max(1e-6, element.typography.size_pt)
            element.typography.size_pt = round(new_size, 2)
            element.typography.leading_pt = round(new_size * ratio, 2)
            element.meta["used_min_size"] = False
            return True

        if action.action == "swap_elements" and element:
            other = page.element(str(action.args.get("element_id_b", "")))
            if other is None:
                return False
            element.rect, other.rect = other.rect, element.rect
            return True

        if action.action == "drop_element" and element:
            page.elements = [e for e in page.elements if e.id != element.id]
            return True

        if action.action == "rebuild_page":
            return self._rebuild(page, str(action.args.get("strategy", "")))

        return False

    def _rebuild(self, page: PageLayout, strategy: str) -> bool:
        """Recompose the page with another strategy, keeping its content."""
        blocks = page.meta.get("blocks")
        if not blocks:
            log.info("Page %d cannot be rebuilt: the source stories were not kept", page.index)
            return False
        from app.layout.strategies import block_from_payload

        try:
            restored = [block_from_payload(block) for block in blocks]
        except (TypeError, ValueError) as exc:
            log.warning("Cannot restore the stories of page %d: %s", page.index, exc)
            return False

        template = self.engine.template
        previous = list(template.layout_rules.allowed_strategies)
        if strategy:
            template.layout_rules.allowed_strategies = [strategy]
        try:
            plan = self.engine.plan_page(
                page.index,
                restored,
                section=page.section,
                page_count=int(page.meta.get("page_count", page.index)),
            )
        finally:
            template.layout_rules.allowed_strategies = previous

        if not plan.page.elements:
            return False
        locked = [e for e in page.elements if e.locked]
        page.elements = locked + [e for e in plan.page.elements if not e.locked]
        page.meta.update(
            {
                "strategy": plan.page.meta.get("strategy"),
                "rebuilt_with": strategy or "auto",
                "blocks": plan.page.meta.get("blocks", blocks),
            }
        )
        return True

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _pull_inside(page: PageLayout, element: ElementSpec) -> dict[str, float]:
        """Delta that brings a straying frame back into the live area."""
        content = page.content_rect
        dx = dy = 0.0
        if element.rect.x < content.x:
            dx = min(40.0, content.x - element.rect.x)
        elif element.rect.right > content.right:
            dx = max(-40.0, content.right - element.rect.right)
        if element.rect.y < content.y:
            dy = min(40.0, content.y - element.rect.y)
        elif element.rect.bottom > content.bottom:
            dy = max(-40.0, content.bottom - element.rect.bottom)
        return {"dx_mm": round(dx, 2), "dy_mm": round(dy, 2)}

    def _under_filled(self, element: ElementSpec) -> bool:
        """Whether a text frame has visibly more room than its copy needs."""
        if not element.is_text or element.typography is None or not element.text.strip():
            return False
        if element.estimated_overflow > 0.001:
            return False
        needed = self.engine.typography.height_for(
            element.text, element.rect.width, element.type, max(1, element.typography.columns)
        )
        return needed < element.rect.height * 0.82

    @staticmethod
    def _neighbour_of_gap(page: PageLayout, gap: Rect | None) -> ElementSpec | None:
        """The frame closest to an empty block, preferring text frames."""
        if gap is None:
            return None
        candidates = [e for e in page.elements if not e.locked and e.type is not ElementType.RULE]
        if not candidates:
            return None
        gx, gy = gap.center

        def distance(element: ElementSpec) -> float:
            ex, ey = element.rect.center
            penalty = 0.0 if element.is_text else 25.0
            return ((ex - gx) ** 2 + (ey - gy) ** 2) ** 0.5 + penalty

        return min(candidates, key=distance)

    @staticmethod
    def _largest_headline(page: PageLayout) -> ElementSpec | None:
        """The headline with the largest type on the page."""
        headlines = [
            e for e in page.elements if e.type is ElementType.HEADLINE and e.typography and not e.locked
        ]
        if not headlines:
            return None
        return max(headlines, key=lambda e: e.typography.size_pt)  # type: ignore[union-attr]

    @staticmethod
    def _second_headline(page: PageLayout) -> ElementSpec | None:
        """The second-largest headline, the one that flattens the hierarchy."""
        headlines = sorted(
            (
                e for e in page.elements
                if e.type is ElementType.HEADLINE and e.typography and not e.locked
            ),
            key=lambda e: -e.typography.size_pt,  # type: ignore[union-attr]
        )
        return headlines[1] if len(headlines) > 1 else None

    def _next_strategy(self, page: PageLayout) -> str:
        """Pick a strategy other than the one that produced this page."""
        allowed = list(self.engine.template.layout_rules.allowed_strategies)
        current = str(page.meta.get("strategy") or "")
        alternatives = [name for name in allowed if name != current]
        return alternatives[0] if alternatives else (allowed[0] if allowed else "modular")


def summarize_text_reduction(element: ElementSpec) -> str:
    """Human readable note about a trimmed frame (used in the QA report)."""
    trimmed = element.meta.get("trimmed_words")
    if not trimmed:
        return ""
    return f"{element.frame_name}: {trimmed} word(s) trimmed ({T.word_count(element.text)} kept)"
