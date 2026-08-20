"""Mouse and keyboard automation (priority 5 - last resort).

This module exists so that an operation which has no scripting or
accessibility path can still be completed, and it is disabled by default:
``settings.adobe.allow_input_automation`` must be turned on explicitly,
because synthetic input affects whatever window happens to be focused.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.errors import AutomationError, PermissionDeniedError

log = logging.getLogger(__name__)


class InputAutomation:
    """Guarded wrapper around ``pyautogui``."""

    def __init__(self, enabled: bool = False, *, move_duration: float = 0.15) -> None:
        self.enabled = enabled
        self.move_duration = move_duration

    def _module(self) -> Any:
        if not self.enabled:
            raise PermissionDeniedError(
                "Mouse/keyboard automation is disabled",
                recovery_action="Enable it in Adobe Settings if this operation really needs it.",
            )
        try:
            import pyautogui  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise AutomationError(
                "pyautogui is not installed; input automation is unavailable", cause=exc
            ) from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui

    def supported(self) -> bool:
        """Whether input automation could run."""
        if not self.enabled:
            return False
        try:
            self._module()
            return True
        except Exception:  # noqa: BLE001
            return False

    def click(self, x: int, y: int, *, clicks: int = 1, button: str = "left") -> bool:
        """Move to ``(x, y)`` and click."""
        pyautogui = self._module()
        log.warning("Input automation: click (%d, %d) x%d", x, y, clicks)
        pyautogui.moveTo(x, y, duration=self.move_duration)
        pyautogui.click(clicks=clicks, button=button)
        return True

    def double_click(self, x: int, y: int) -> bool:
        """Double-click at ``(x, y)``."""
        return self.click(x, y, clicks=2)

    def type_text(self, text: str, *, interval: float = 0.01) -> bool:
        """Type *text* into the focused control."""
        pyautogui = self._module()
        log.warning("Input automation: typing %d character(s)", len(text))
        pyautogui.typewrite(text, interval=interval)
        return True

    def hotkey(self, *keys: str) -> bool:
        """Press a key combination such as ``("ctrl", "e")``."""
        pyautogui = self._module()
        log.warning("Input automation: hotkey %s", "+".join(keys))
        pyautogui.hotkey(*keys)
        return True

    def press(self, key: str, times: int = 1) -> bool:
        """Press a single key *times* times."""
        pyautogui = self._module()
        for _ in range(times):
            pyautogui.press(key)
            time.sleep(0.03)
        return True

    def screen_size(self) -> tuple[int, int]:
        """Primary screen resolution."""
        pyautogui = self._module()
        size = pyautogui.size()
        return (int(size[0]), int(size[1]))

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return {"enabled": self.enabled, "supported": self.supported()}
