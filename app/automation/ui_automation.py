"""Windows UI Automation fallback (priority 4).

Used only when neither the scripting API nor the file-based queue can reach
the host application. It drives the real UI through the accessibility tree
(``pywinauto`` with the UIA backend), which is far more robust than clicking
at coordinates but still depends on menu names and dialog layout - hence its
position near the bottom of the chain.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

from app.core.errors import AutomationError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

WINDOW_PATTERNS = {
    "indesign": r".*Adobe InDesign.*",
    "photoshop": r".*Adobe Photoshop.*",
}

#: Menu paths differ between localisations; the English ones are tried first
#: and the operator can override them in Adobe Settings.
MENU_PATHS = {
    "indesign": {
        "scripts_panel": "Window->Utilities->Scripts",
        "open": "File->Open...",
        "save_as": "File->Save As...",
        "export": "File->Export...",
        "close": "File->Close",
    },
    "photoshop": {
        "scripts": "File->Scripts->Browse...",
        "open": "File->Open...",
        "save_as": "File->Save As...",
        "close": "File->Close",
    },
}


class UIAutomation:
    """Thin, defensive wrapper around ``pywinauto``."""

    def __init__(self, host: str, timeout: float = 45.0) -> None:
        self.host = host
        self.timeout = timeout
        self._app: Any = None
        self._window: Any = None

    # --------------------------------------------------------- availability
    @staticmethod
    def supported() -> bool:
        """Whether UI automation can run on this machine."""
        if not IS_WINDOWS:
            return False
        try:
            import pywinauto  # type: ignore  # noqa: F401

            return True
        except Exception:  # pragma: no cover - Windows only
            return False

    def available(self) -> bool:
        """Whether the host application window can be found."""
        if not self.supported():
            return False
        try:
            return self.window() is not None
        except Exception as exc:  # noqa: BLE001
            log.debug("UI automation unavailable: %s", exc)
            return False

    # ------------------------------------------------------------- window
    def window(self) -> Any:
        """Return the host's main window, connecting on first use."""
        if self._window is not None:
            return self._window
        if not self.supported():
            raise AutomationError("UI automation requires Windows and pywinauto")
        from pywinauto import Desktop  # type: ignore

        pattern = WINDOW_PATTERNS.get(self.host)
        if pattern is None:
            raise AutomationError(f"No window pattern known for host '{self.host}'")
        desktop = Desktop(backend="uia")
        deadline = time.monotonic() + self.timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                window = desktop.window(title_re=pattern)
                if window.exists():
                    window.wait("exists ready", timeout=5)
                    self._window = window
                    return window
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(1.0)
        raise AutomationError(
            f"Could not find the {self.host} window",
            context={"pattern": pattern, "last_error": str(last)[:200]},
        )

    def focus(self) -> None:
        """Bring the host window to the foreground."""
        window = self.window()
        try:
            window.set_focus()
        except Exception as exc:  # noqa: BLE001
            log.debug("set_focus failed: %s", exc)

    # -------------------------------------------------------------- menus
    def menu(self, key: str) -> bool:
        """Invoke a named menu entry."""
        path = MENU_PATHS.get(self.host, {}).get(key)
        if not path:
            raise AutomationError(f"No menu path configured for '{key}' on {self.host}")
        window = self.window()
        self.focus()
        try:
            window.menu_select(path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Menu '%s' could not be invoked: %s", path, exc)
            return False

    # ------------------------------------------------------------ dialogs
    def type_path_and_confirm(self, path: Path | str, *, confirm: str = "{ENTER}") -> bool:
        """Type a path into the focused file dialog and confirm it."""
        from pywinauto.keyboard import send_keys  # type: ignore

        text = str(path).replace("(", "{(}").replace(")", "{)}").replace("+", "{+}")
        text = text.replace("^", "{^}").replace("%", "{%}").replace("~", "{~}")
        send_keys(text, with_spaces=True, pause=0.01)
        time.sleep(0.4)
        send_keys(confirm)
        return True

    def wait_for_dialog(self, title_re: str, timeout: float | None = None) -> Any:
        """Wait for a dialog whose title matches *title_re*."""
        from pywinauto import Desktop  # type: ignore

        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            try:
                dialog = Desktop(backend="uia").window(title_re=title_re)
                if dialog.exists():
                    return dialog
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        raise AutomationError(f"Dialog '{title_re}' did not appear", context={"host": self.host})

    def click_button(self, dialog: Any, names: list[str]) -> bool:
        """Click the first button in *dialog* matching one of *names*."""
        for name in names:
            try:
                button = dialog.child_window(title=name, control_type="Button")
                if button.exists():
                    button.click_input()
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # --------------------------------------------------------- operations
    def open_file(self, path: Path | str) -> bool:
        """Open a document through ``File -> Open``."""
        if not self.menu("open"):
            from pywinauto.keyboard import send_keys  # type: ignore

            self.focus()
            send_keys("^o")
        time.sleep(1.2)
        self.type_path_and_confirm(path)
        time.sleep(2.0)
        return True

    def run_script_file(self, path: Path | str) -> bool:
        """Run a JSX file through the host's script browser.

        InDesign has no "run this file" menu item, so the caller is expected to
        have placed the script in the Scripts Panel folder; this method opens
        the panel so the operator can see it and, where the tree is exposed
        through UIA, double-clicks the entry.
        """
        name = Path(path).name
        if self.host == "photoshop":
            if not self.menu("scripts"):
                return False
            time.sleep(1.0)
            self.type_path_and_confirm(path)
            return True
        if not self.menu("scripts_panel"):
            return False
        time.sleep(1.0)
        try:
            window = self.window()
            item = window.child_window(title_re=rf".*{name.split('.')[0]}.*")
            if item.exists():
                item.double_click_input()
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not double-click the script entry: %s", exc)
        return False

    def export_pdf(self, path: Path | str, preset: str | None = None) -> bool:
        """Export a PDF through ``File -> Export``."""
        if self.host != "indesign":
            raise AutomationError("PDF export through the UI is only implemented for InDesign")
        if not self.menu("export"):
            from pywinauto.keyboard import send_keys  # type: ignore

            self.focus()
            send_keys("^e")
        time.sleep(1.2)
        self.type_path_and_confirm(path)
        time.sleep(1.5)
        try:
            dialog = self.wait_for_dialog(r".*Export Adobe PDF.*", timeout=20)
            if preset:
                try:
                    combo = dialog.child_window(control_type="ComboBox", found_index=0)
                    combo.select(preset)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Could not select the PDF preset '%s': %s", preset, exc)
            self.click_button(dialog, ["Export", "OK", "Save"])
        except AutomationError:
            log.warning("The Export Adobe PDF dialog did not appear; assuming a direct export")
        time.sleep(3.0)
        return Path(path).exists()

    def save_as(self, path: Path | str) -> bool:
        """Save the active document under a new name."""
        if not self.menu("save_as"):
            from pywinauto.keyboard import send_keys  # type: ignore

            self.focus()
            send_keys("^+s")
        time.sleep(1.2)
        self.type_path_and_confirm(path)
        time.sleep(2.5)
        return Path(path).exists()

    def close_document(self) -> bool:
        """Close the active document, discarding changes."""
        if not self.menu("close"):
            from pywinauto.keyboard import send_keys  # type: ignore

            self.focus()
            send_keys("^w")
        time.sleep(0.8)
        try:
            dialog = self.wait_for_dialog(r".*(Adobe InDesign|Adobe Photoshop).*", timeout=5)
            self.click_button(dialog, ["Don't Save", "No", "Discard"])
        except AutomationError:
            pass
        return True

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return {
            "host": self.host,
            "supported": self.supported(),
            "available": self.available() if self.supported() else False,
            "menus": MENU_PATHS.get(self.host, {}),
        }
