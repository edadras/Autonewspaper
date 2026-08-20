"""The automation priority chain.

Specification §2 requires that every Adobe operation is attempted through the
most reliable mechanism first and only degrades when that mechanism is
unavailable. The scripting strategies live in :mod:`app.adobe.bridge`; this
module adds the last two rungs of the ladder and provides the router that ties
them together for the few operations that have a UI-only path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Generic, TypeVar

from app.core.errors import AutomationError

log = logging.getLogger(__name__)

T = TypeVar("T")


class Tier(IntEnum):
    """Priority of an automation mechanism (lower is preferred)."""

    SCRIPTING_API = 1
    EXTENDSCRIPT_FILE = 2
    FILE_BASED = 3
    UI_AUTOMATION = 4
    INPUT_AUTOMATION = 5

    @property
    def label(self) -> str:
        """Human readable name used in logs and the UI."""
        return {
            Tier.SCRIPTING_API: "Adobe scripting API",
            Tier.EXTENDSCRIPT_FILE: "ExtendScript file",
            Tier.FILE_BASED: "File-based queue",
            Tier.UI_AUTOMATION: "Windows UI Automation",
            Tier.INPUT_AUTOMATION: "Mouse/keyboard automation",
        }[self]


@dataclass
class Attempt:
    """Record of one attempt at performing an operation."""

    tier: Tier
    ok: bool
    detail: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "tier": int(self.tier),
            "mechanism": self.tier.label,
            "ok": self.ok,
            "detail": self.detail,
            "duration": round(self.duration, 3),
        }


@dataclass
class Handler(Generic[T]):
    """One way of performing an operation."""

    tier: Tier
    run: Callable[[], T]
    can_run: Callable[[], bool] = field(default=lambda: True)
    name: str = ""

    def label(self) -> str:
        """Name shown in logs."""
        return self.name or self.tier.label


@dataclass
class OperationOutcome(Generic[T]):
    """Result of routing one operation."""

    value: T
    tier: Tier
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """Whether a lower-priority mechanism had to be used."""
        return self.tier > Tier.SCRIPTING_API

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "tier": int(self.tier),
            "mechanism": self.tier.label,
            "degraded": self.degraded,
            "attempts": [a.to_dict() for a in self.attempts],
        }


class OperationRouter:
    """Runs an operation through the first mechanism that works."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.handlers: list[Handler[Any]] = []

    def add(
        self,
        tier: Tier,
        run: Callable[[], T],
        *,
        can_run: Callable[[], bool] | None = None,
        name: str = "",
    ) -> OperationRouter:
        """Register a handler for *tier*."""
        self.handlers.append(Handler(tier=tier, run=run, can_run=can_run or (lambda: True), name=name))
        self.handlers.sort(key=lambda h: h.tier)
        return self

    def execute(self) -> OperationOutcome[Any]:
        """Try each handler in priority order."""
        attempts: list[Attempt] = []
        for handler in self.handlers:
            started = time.monotonic()
            try:
                if not handler.can_run():
                    attempts.append(
                        Attempt(handler.tier, False, "mechanism unavailable", time.monotonic() - started)
                    )
                    continue
            except Exception as exc:  # noqa: BLE001
                attempts.append(Attempt(handler.tier, False, f"probe failed: {exc}", 0.0))
                continue
            try:
                value = handler.run()
            except Exception as exc:  # noqa: BLE001 - degrade to the next tier
                duration = time.monotonic() - started
                attempts.append(Attempt(handler.tier, False, str(exc)[:300], duration))
                log.warning("%s via %s failed: %s", self.operation, handler.label(), str(exc)[:200])
                continue
            duration = time.monotonic() - started
            attempts.append(Attempt(handler.tier, True, "", duration))
            if handler.tier > Tier.SCRIPTING_API:
                log.warning(
                    "%s completed through the fallback mechanism '%s'",
                    self.operation,
                    handler.label(),
                )
            return OperationOutcome(value=value, tier=handler.tier, attempts=attempts)

        raise AutomationError(
            f"Every automation mechanism failed for '{self.operation}'",
            context={"attempts": [a.to_dict() for a in attempts]},
        )
