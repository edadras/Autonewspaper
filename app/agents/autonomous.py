"""The bounded autonomous agent.

Implements specification §56 and §57: the agent may only use the registered
tools, and every loop is bounded by a maximum number of iterations, a wall
clock timeout and a retry limit, so it can never spin.

The agent works with any configured provider. When the provider is the
offline analyser - or when a model reply cannot be parsed - the agent falls
back to a deterministic plan built from the QA report, so the loop still makes
real progress without a cloud model.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai.base import ChatMessage, TextRequest
from app.ai.registry import AIService
from app.agents.tools import ToolRegistry, ToolResult
from app.core.errors import AppError, Component
from app.core.jobs import CancelToken

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the production operator of a newspaper page.

You work only through the tools listed below. You never describe actions in
prose and you never invent a tool. Every reply is a single JSON object:

{"thought": "one short sentence", "tool": "<tool name>", "arguments": {...}}

When the goal is reached, reply with:

{"thought": "why the goal is met", "done": true, "summary": "what you changed"}

Rules:
- Inspect before you change: analyze_page tells you what is actually wrong.
- Make the smallest change that fixes the reported issue.
- A tool that returns an error means the change was refused; read the error,
  correct your arguments and try something else. Do not repeat a failing call.
- You may not move or resize locked frames (master-page furniture).

Available tools:
{tools}
"""


@dataclass
class AgentStep:
    """One turn of the agent loop."""

    index: int
    thought: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "index": self.index,
            "thought": self.thought,
            "tool": self.tool,
            "arguments": self.arguments,
            "result": self.result.to_dict() if self.result else None,
            "done": self.done,
        }


@dataclass
class AgentRun:
    """The outcome of an agent loop."""

    goal: str
    steps: list[AgentStep] = field(default_factory=list)
    finished: bool = False
    summary: str = ""
    stop_reason: str = ""
    duration: float = 0.0

    @property
    def tool_calls(self) -> int:
        """How many tools were actually invoked."""
        return sum(1 for step in self.steps if step.result is not None)

    @property
    def failures(self) -> int:
        """How many calls were refused or failed."""
        return sum(1 for step in self.steps if step.result and not step.result.ok)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "goal": self.goal,
            "finished": self.finished,
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "failures": self.failures,
            "duration": round(self.duration, 2),
            "steps": [step.to_dict() for step in self.steps],
        }


class AutonomousAgent:
    """Drives a goal to completion through the tool registry."""

    def __init__(
        self,
        ai: AIService,
        registry: ToolRegistry,
        *,
        max_iterations: int = 12,
        timeout_seconds: float = 900.0,
        max_retries: int = 3,
        fallback: Callable[[list[AgentStep]], AgentStep | None] | None = None,
    ) -> None:
        self.ai = ai
        self.registry = registry
        self.max_iterations = max(1, max_iterations)
        self.timeout_seconds = max(10.0, timeout_seconds)
        self.max_retries = max(0, max_retries)
        self.fallback = fallback

    # ---------------------------------------------------------------- run
    def run(
        self,
        goal: str,
        *,
        context: str = "",
        token: CancelToken | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> AgentRun:
        """Pursue *goal* until it is met or a bound is reached."""
        started = time.monotonic()
        run = AgentRun(goal=goal)
        transcript: list[ChatMessage] = [
            ChatMessage("system", SYSTEM_PROMPT.replace("{tools}", self.registry.describe())),
            ChatMessage("user", _initial_message(goal, context)),
        ]
        consecutive_failures = 0
        seen_calls: set[str] = set()

        for index in range(1, self.max_iterations + 1):
            if token is not None and token.cancelled:
                run.stop_reason = "cancelled"
                break
            if time.monotonic() - started > self.timeout_seconds:
                run.stop_reason = "timeout"
                log.warning("Agent timed out after %.0fs on goal: %s", self.timeout_seconds, goal)
                break

            step = self._next_step(index, transcript, run)
            if step is None:
                run.stop_reason = "no actionable step"
                break
            if step.done:
                run.steps.append(step)
                run.finished = True
                run.summary = step.thought
                run.stop_reason = "goal reached"
                if on_step:
                    on_step(step)
                break

            signature = f"{step.tool}:{json.dumps(step.arguments, sort_keys=True, default=str)}"
            # Inspection tools may be repeated: re-reading the page after a
            # change is exactly how the agent learns whether the change worked.
            if signature in seen_calls and not self._is_inspection(step.tool):
                message = (
                    f"The call {step.tool} with those exact arguments was already made and "
                    "did not help; choose a different action."
                )
                transcript.append(ChatMessage("assistant", json.dumps(step.to_dict())))
                transcript.append(ChatMessage("user", message))
                consecutive_failures += 1
                if consecutive_failures > self.max_retries:
                    run.stop_reason = "repeated identical calls"
                    break
                continue
            seen_calls.add(signature)

            step.result = self.registry.invoke(step.tool, step.arguments)
            run.steps.append(step)
            if on_step:
                on_step(step)

            transcript.append(ChatMessage("assistant", json.dumps({
                "thought": step.thought, "tool": step.tool, "arguments": step.arguments
            }, ensure_ascii=False)))
            transcript.append(ChatMessage("user", json.dumps(step.result.to_dict(), ensure_ascii=False, default=str)))

            if step.result.ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures > self.max_retries:
                    run.stop_reason = f"{consecutive_failures} consecutive failures"
                    log.warning("Agent stopped: %s", run.stop_reason)
                    break
        else:
            run.stop_reason = "iteration limit reached"

        run.duration = time.monotonic() - started
        if not run.stop_reason:
            run.stop_reason = "iteration limit reached"
        log.info(
            "Agent finished '%s': %s after %d call(s) in %.1fs",
            goal, run.stop_reason, run.tool_calls, run.duration,
        )
        return run

    def _is_inspection(self, tool_name: str) -> bool:
        """Whether a tool only reads state and may therefore be repeated."""
        tool = self.registry._tools.get(tool_name)  # noqa: SLF001 - internal lookup
        return bool(tool and tool.capability.endswith(".read"))

    # -------------------------------------------------------------- steps
    def _next_step(
        self, index: int, transcript: list[ChatMessage], run: AgentRun
    ) -> AgentStep | None:
        """Ask the model for the next action, or fall back to the planner."""
        request = TextRequest(
            messages=list(transcript),
            temperature=0.1,
            max_tokens=800,
            json_mode=True,
            metadata={"task": "agent_step", "data": {"goal": run.goal}},
        )
        payload: Any = None
        try:
            response = self.ai._complete(request, purpose="agent step")  # noqa: SLF001
            payload = response.json(required=False)
        except AppError as exc:
            log.warning("Agent step %d: provider error (%s)", index, exc.message)
        except Exception as exc:  # noqa: BLE001
            log.warning("Agent step %d failed: %s", index, exc)

        step = _parse_step(index, payload)
        if step is not None:
            return step
        if self.fallback is not None:
            fallback_step = self.fallback(run.steps)
            if fallback_step is not None:
                fallback_step.index = index
                log.info("Agent step %d taken from the deterministic planner", index)
                return fallback_step
        return None


def _parse_step(index: int, payload: Any) -> AgentStep | None:
    """Turn a model reply into an :class:`AgentStep`."""
    if not isinstance(payload, dict):
        return None
    if payload.get("done") is True:
        return AgentStep(
            index=index,
            thought=str(payload.get("summary") or payload.get("thought") or "goal reached"),
            done=True,
        )
    tool = payload.get("tool")
    if not tool:
        return None
    arguments = payload.get("arguments")
    return AgentStep(
        index=index,
        thought=str(payload.get("thought", ""))[:400],
        tool=str(tool),
        arguments=arguments if isinstance(arguments, dict) else {},
    )


def _initial_message(goal: str, context: str) -> str:
    """First user turn of the agent conversation."""
    parts = [f"Goal: {goal}"]
    if context:
        parts.append(f"Current state:\n{context}")
    parts.append("Reply with a single JSON object choosing your first tool call.")
    return "\n\n".join(parts)


def qa_fallback_planner(page_index: int) -> Callable[[list[AgentStep]], AgentStep | None]:
    """A deterministic planner used when no model is driving the agent.

    It inspects the page, then derives concrete tool calls from the measured
    geometry the QA report returns, so the agent loop does real work offline as
    well as with a cloud model.
    """

    def plan(steps: list[AgentStep]) -> AgentStep | None:
        analysed = [s for s in steps if s.tool == "analyze_page" and s.result and s.result.ok]
        if not analysed:
            return AgentStep(
                index=0, thought="Inspect the page before changing anything",
                tool="analyze_page", arguments={"page": page_index},
            )
        data = analysed[-1].result.data or {}  # type: ignore[union-attr]
        if data.get("passed"):
            return AgentStep(index=0, thought="The page passes quality assurance", done=True)

        elements = {e["id"]: e for e in (data.get("elements") or [])}
        live = data.get("live_area") or {}
        tried = {(s.tool, json.dumps(s.arguments, sort_keys=True)) for s in steps}

        for issue in data.get("issues") or []:
            element_id = issue.get("element_id")
            element = elements.get(element_id) if element_id else None
            if element is None or element.get("locked"):
                continue
            candidate: AgentStep | None = None

            if issue.get("type") == "text_overflow":
                overflow = float(element.get("overflow") or 0.1)
                new_height = element["height"] * (1.0 + min(0.25, max(0.05, overflow)))
                bottom_limit = float(live.get("y", 0)) + float(live.get("height", 0))
                new_height = min(new_height, max(10.0, bottom_limit - element["y"]))
                candidate = AgentStep(
                    index=0,
                    thought=f"Enlarge '{element_id}' so its copy fits",
                    tool="resize_element",
                    arguments={
                        "element_id": element_id,
                        "width_mm": round(element["width"], 2),
                        "height_mm": round(new_height, 2),
                    },
                )
            elif issue.get("type") in ("margin_violation", "out_of_bounds"):
                x = min(
                    max(element["x"], float(live.get("x", 0))),
                    float(live.get("x", 0)) + float(live.get("width", 0)) - element["width"],
                )
                y = min(
                    max(element["y"], float(live.get("y", 0))),
                    float(live.get("y", 0)) + float(live.get("height", 0)) - element["height"],
                )
                candidate = AgentStep(
                    index=0,
                    thought=f"Bring '{element_id}' back inside the live area",
                    tool="move_element",
                    arguments={
                        "element_id": element_id,
                        "x_mm": round(max(0.0, x), 2),
                        "y_mm": round(max(0.0, y), 2),
                    },
                )
            elif issue.get("type") == "empty_frame":
                candidate = AgentStep(
                    index=0,
                    thought=f"'{element_id}' renders blank; re-insert its copy",
                    tool="insert_text",
                    arguments={"element_id": element_id, "text": issue.get("message", "")[:80]},
                )

            if candidate is None:
                continue
            key = (candidate.tool, json.dumps(candidate.arguments, sort_keys=True))
            if key not in tried:
                return candidate

        # Nothing actionable is left. Re-inspect only if something was actually
        # changed since the last inspection; otherwise another analyze_page
        # would return the same report and the loop would spin.
        last_analysis_at = max(
            (i for i, s in enumerate(steps) if s.tool == "analyze_page"), default=-1
        )
        changed_since = any(
            s.tool != "analyze_page" and s.result is not None and s.result.ok
            for s in steps[last_analysis_at + 1 :]
        )
        if changed_since:
            return AgentStep(
                index=0, thought="Re-inspect the page after the corrections",
                tool="analyze_page", arguments={"page": page_index},
            )
        return AgentStep(
            index=0,
            thought=(
                "No issue names a frame this agent can correct; the page needs an "
                "editorial decision (more copy or fewer pages)"
            ),
            done=True,
        )

    return plan
