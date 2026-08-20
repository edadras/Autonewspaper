"""The agent tool surface.

Specification §55 is the rule this module enforces: the AI never touches the
operating system directly. It may only call the tools registered here, and
every call goes through the same five steps -

``validate -> permission -> execute -> verify -> report``

An argument outside its schema, or a capability the operator has not granted,
is refused before anything runs, and the result of every call is verified
against the system's own state rather than trusted from the model.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core.errors import PermissionDeniedError, ToolValidationError
from app.core.events import EventBus, EventType

log = logging.getLogger(__name__)

JsonType = Literal["string", "number", "integer", "boolean", "object", "array"]


@dataclass
class Parameter:
    """One tool argument and the rules it must satisfy."""

    name: str
    type: JsonType
    description: str = ""
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: list[Any] | None = None
    default: Any = None

    def schema(self) -> dict[str, Any]:
        """JSON-schema fragment for this parameter."""
        out: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.choices:
            out["enum"] = self.choices
        return out

    def coerce(self, value: Any) -> Any:
        """Convert and range-check a supplied value."""
        if self.type == "integer":
            value = int(round(float(value)))
        elif self.type == "number":
            value = float(value)
        elif self.type == "boolean":
            value = bool(value) if not isinstance(value, str) else value.lower() in ("true", "1", "yes")
        elif self.type == "string":
            value = str(value)
        elif self.type == "array" and not isinstance(value, list):
            raise ToolValidationError(f"'{self.name}' must be an array")
        elif self.type == "object" and not isinstance(value, dict):
            raise ToolValidationError(f"'{self.name}' must be an object")

        if self.minimum is not None and float(value) < self.minimum:
            raise ToolValidationError(f"'{self.name}'={value} is below the minimum {self.minimum}")
        if self.maximum is not None and float(value) > self.maximum:
            raise ToolValidationError(f"'{self.name}'={value} is above the maximum {self.maximum}")
        if self.choices and value not in self.choices:
            raise ToolValidationError(f"'{self.name}'={value!r} is not one of {self.choices}")
        return value


@dataclass
class ToolResult:
    """Outcome of a tool call."""

    tool: str
    ok: bool
    data: Any = None
    error: str = ""
    verified: bool | None = None
    verification: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (this is what the model sees)."""
        return {
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "verified": self.verified,
            "verification": self.verification,
            "duration": round(self.duration, 3),
        }


@dataclass
class Tool:
    """A capability the agent may use."""

    name: str
    description: str
    parameters: list[Parameter]
    handler: Callable[..., Any]
    capability: str = "general"
    verifier: Callable[[Any, dict[str, Any]], tuple[bool, str]] | None = None
    destructive: bool = False

    def schema(self) -> dict[str, Any]:
        """OpenAI/Anthropic-style function schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {p.name: p.schema() for p in self.parameters},
                "required": [p.name for p in self.parameters if p.required],
            },
        }

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check and coerce *arguments*; raises on anything invalid."""
        if not isinstance(arguments, dict):
            raise ToolValidationError(f"{self.name}: arguments must be an object")
        known = {p.name for p in self.parameters}
        unknown = set(arguments) - known
        if unknown:
            raise ToolValidationError(
                f"{self.name}: unknown argument(s) {', '.join(sorted(unknown))}",
                context={"allowed": sorted(known)},
            )
        cleaned: dict[str, Any] = {}
        for parameter in self.parameters:
            if parameter.name in arguments:
                cleaned[parameter.name] = parameter.coerce(arguments[parameter.name])
            elif parameter.required:
                raise ToolValidationError(f"{self.name}: missing argument '{parameter.name}'")
            elif parameter.default is not None:
                cleaned[parameter.name] = parameter.default
        return cleaned


class PermissionPolicy:
    """Which capabilities the agent is allowed to use."""

    ALL = {
        "adobe.read",
        "adobe.write",
        "adobe.launch",
        "layout.read",
        "layout.write",
        "assets.read",
        "assets.write",
        "ai.generate_image",
        "export.write",
        "project.write",
    }

    def __init__(self, granted: set[str] | None = None) -> None:
        self.granted = set(granted) if granted is not None else set(self.ALL)

    def allows(self, capability: str) -> bool:
        """Whether *capability* has been granted."""
        return capability in self.granted or "*" in self.granted

    def grant(self, capability: str) -> None:
        """Grant a capability."""
        self.granted.add(capability)

    def revoke(self, capability: str) -> None:
        """Remove a capability."""
        self.granted.discard(capability)

    @classmethod
    def read_only(cls) -> PermissionPolicy:
        """A policy that permits inspection but no modification."""
        return cls({c for c in cls.ALL if c.endswith(".read")})

    def describe(self) -> dict[str, Any]:
        """Summary for the log and the UI."""
        return {"granted": sorted(self.granted), "denied": sorted(self.ALL - self.granted)}


class ToolRegistry:
    """Holds the tools and enforces the call protocol."""

    def __init__(self, policy: PermissionPolicy | None = None, bus: EventBus | None = None) -> None:
        self.policy = policy or PermissionPolicy()
        self.bus = bus
        self._tools: dict[str, Tool] = {}
        self.history: list[ToolResult] = []

    def register(self, tool: Tool) -> Tool:
        """Add a tool."""
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        """Look a tool up by name."""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolValidationError(
                f"Unknown tool '{name}'",
                context={"available": sorted(self._tools)},
                recovery_action="The agent must choose one of the registered tools.",
            )
        return tool

    def names(self) -> list[str]:
        """Every registered tool name."""
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """Function schemas for the tools the policy currently permits."""
        return [tool.schema() for tool in self._tools.values() if self.policy.allows(tool.capability)]

    def describe(self) -> str:
        """Human-readable tool list, embedded in the agent prompt."""
        lines = []
        for tool in sorted(self._tools.values(), key=lambda t: t.name):
            if not self.policy.allows(tool.capability):
                continue
            args = ", ".join(f"{p.name}: {p.type}" + ("" if p.required else "?") for p in tool.parameters)
            lines.append(f"- {tool.name}({args}) - {tool.description}")
        return "\n".join(lines)

    # -------------------------------------------------------------- invoke
    def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Run the full validate/permit/execute/verify protocol."""
        started = time.monotonic()
        arguments = arguments or {}
        try:
            tool = self.get(name)
        except ToolValidationError as exc:
            return self._record(ToolResult(name, False, error=exc.message, duration=0.0))

        try:
            cleaned = tool.validate(arguments)
        except ToolValidationError as exc:
            log.warning("Tool '%s' rejected: %s", name, exc.message)
            return self._record(
                ToolResult(name, False, error=exc.message, duration=time.monotonic() - started)
            )

        if not self.policy.allows(tool.capability):
            message = f"The capability '{tool.capability}' required by '{name}' is not granted"
            log.warning("%s", message)
            return self._record(ToolResult(name, False, error=message, duration=time.monotonic() - started))

        self._emit(EventType.ADOBE_COMMAND, tool=name, arguments=cleaned)
        try:
            data = tool.handler(**cleaned)
        except PermissionDeniedError as exc:
            return self._record(
                ToolResult(name, False, error=exc.message, duration=time.monotonic() - started)
            )
        except Exception as exc:  # noqa: BLE001 - a failing tool must not kill the agent
            log.error("Tool '%s' failed: %s", name, exc, exc_info=exc)
            return self._record(
                ToolResult(
                    name,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    duration=time.monotonic() - started,
                )
            )

        verified, verification = (None, "")
        if tool.verifier is not None:
            try:
                verified, verification = tool.verifier(data, cleaned)
            except Exception as exc:  # noqa: BLE001
                verified, verification = (False, f"verification failed: {exc}")
            if verified is False:
                log.warning("Tool '%s' could not be verified: %s", name, verification)

        return self._record(
            ToolResult(
                tool=name,
                ok=verified is not False,
                data=data,
                verified=verified,
                verification=verification,
                duration=time.monotonic() - started,
            )
        )

    def _record(self, result: ToolResult) -> ToolResult:
        self.history.append(result)
        log.info(
            "tool %s -> %s%s",
            result.tool,
            "ok" if result.ok else f"error: {result.error[:120]}",
            f" ({result.verification})" if result.verification else "",
        )
        return result

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)

    def stats(self) -> dict[str, Any]:
        """Call counts, used by the agent's stop conditions and the UI."""
        return {
            "calls": len(self.history),
            "failures": sum(1 for r in self.history if not r.ok),
            "by_tool": {name: sum(1 for r in self.history if r.tool == name) for name in self.names()},
        }


# ------------------------------------------------------------- verifiers --


def verify_exists(data: Any, _arguments: dict[str, Any]) -> tuple[bool, str]:
    """Confirm that a tool which claims to have written a file really did."""
    path = None
    if isinstance(data, str):
        path = Path(data)
    elif isinstance(data, dict):
        candidate = data.get("path") or data.get("target")
        path = Path(candidate) if candidate else None
    if path is None:
        return (False, "the tool returned no path to verify")
    if not path.exists():
        return (False, f"{path} was not created")
    return (True, f"{path.name} exists ({path.stat().st_size} bytes)")


def verify_truthy(data: Any, _arguments: dict[str, Any]) -> tuple[bool, str]:
    """Confirm that an operation reported success."""
    if isinstance(data, dict) and "error" in data and data["error"]:
        return (False, str(data["error"])[:200])
    return (bool(data), "" if data else "the operation reported no result")


def verify_geometry(data: Any, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Confirm a frame really ended up where the agent asked."""
    if not isinstance(data, dict) or "x" not in data:
        return (False, "no geometry was returned")
    tolerance = 0.75
    for key, argument in (("x", "x_mm"), ("y", "y_mm"), ("width", "width_mm"), ("height", "height_mm")):
        if argument in arguments:
            actual, wanted = float(data[key]), float(arguments[argument])
            if abs(actual - wanted) > tolerance:
                return (
                    False,
                    f"{key} is {actual:.2f} mm but {wanted:.2f} mm was requested",
                )
    return (True, f"frame at ({data['x']:.1f}, {data['y']:.1f}) mm")
