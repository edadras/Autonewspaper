"""Base class for the pages of the main window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.bridge import EventBridge

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

    def __init__(
        self, application: Application, bridge: EventBridge, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.app = application
        self.bridge = bridge
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

    def placeholder(self, message: str) -> QLabel:
        """A centred hint shown when there is nothing to display."""
        label = QLabel(message)
        label.setObjectName("Subtitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        return label
