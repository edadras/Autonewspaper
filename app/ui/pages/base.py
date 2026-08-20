"""Base class for the pages of the main window."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.bridge import EventBridge
from app.ui.tasks import BackgroundTask, TaskRunner

if TYPE_CHECKING:  # pragma: no cover
    from app.application import Application

log = logging.getLogger(__name__)


class Page(QWidget):
    """One navigable page.

    Subclasses build their widgets in :meth:`build` and refresh their contents
    in :meth:`refresh`, which the main window calls when the page is shown and
    whenever the current project changes.
    """

    title = "Page"
    subtitle = ""
    icon = "•"

    def __init__(self, application: Application, bridge: EventBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = application
        self.bridge = bridge
        self.tasks = TaskRunner(self)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(20, 18, 20, 18)
        self.root.setSpacing(14)
        self._add_header()
        self.build()

    def _add_header(self) -> None:
        heading = QLabel(self.title)
        heading.setObjectName("Title")
        self.root.addWidget(heading)
        if self.subtitle:
            caption = QLabel(self.subtitle)
            caption.setObjectName("Subtitle")
            caption.setWordWrap(True)
            self.root.addWidget(caption)

    def build(self) -> None:
        """Create the page's widgets."""

    def refresh(self) -> None:
        """Reload the page's data."""

    # ----------------------------------------------------------- helpers
    @property
    def handle(self):
        """The open project, or ``None``."""
        return self.app.current

    def require_project(self) -> bool:
        """Whether a project is open; shows a hint when it is not."""
        return self.app.current is not None

    def run_background(
        self,
        key: str,
        title: str,
        operation: Callable[..., Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        wants_progress: bool = False,
        busy_widgets: list[QWidget] | None = None,
        status: QLabel | None = None,
    ) -> BackgroundTask | None:
        """Run *operation* off the GUI thread, reporting into this page.

        The triggering widgets are disabled while it runs and re-enabled
        whichever way it ends, so a long import can never leave the page in a
        half-usable state.
        """
        from app.ui.widgets.common import show_error

        widgets = busy_widgets or []
        for widget in widgets:
            widget.setEnabled(False)
        if status is not None:
            status.setText(f"{title}…")

        def _restore() -> None:
            for widget in widgets:
                widget.setEnabled(True)

        def _failed(message: str, detail: str) -> None:
            if status is not None:
                status.setText(f"{title} failed: {message[:160]}")
            show_error(self, title, message[:400], detail)

        task = self.tasks.start(
            key,
            operation,
            title=title,
            wants_progress=wants_progress,
            on_success=on_success,
            on_failure=_failed,
            on_progress=(lambda text: status.setText(text)) if status is not None else None,
            on_finished=_restore,
        )
        if task is None:
            _restore()
            if status is not None:
                status.setText(f"{title} is already running.")
        return task

    def placeholder(self, message: str) -> QLabel:
        """A centred hint shown when there is nothing to display."""
        label = QLabel(message)
        label.setObjectName("Subtitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        return label
