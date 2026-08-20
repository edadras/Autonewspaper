"""Reusable widgets shared by the pages."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


class Card(QFrame):
    """A titled panel."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setStyleSheet("font-weight: 600;")
            self._layout.addWidget(label)

    def body(self) -> QVBoxLayout:
        """The layout callers add their content to."""
        return self._layout

    def add(self, widget: QWidget) -> QWidget:
        """Append a widget and return it."""
        self._layout.addWidget(widget)
        return widget


class MetricTile(Card):
    """A single number with a caption, used on the Dashboard."""

    def __init__(self, caption: str, value: str = "-", parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("Metric")
        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("MetricLabel")
        self._layout.addWidget(self.value_label)
        self._layout.addWidget(self.caption_label)
        self.setMinimumWidth(140)

    def set_value(self, value: Any) -> None:
        """Update the displayed number."""
        self.value_label.setText(str(value))


class StatusPill(QLabel):
    """A small coloured status label."""

    def __init__(self, text: str = "", color: str = "#666", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.set_status(text, color)

    def set_status(self, text: str, color: str) -> None:
        """Change the text and colour."""
        self.setText(text)
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 9px;"
            " padding: 2px 9px; font-size: 11px; font-weight: 600;"
        )
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)


class DropArea(QFrame):
    """A drop target for files (specification §22)."""

    files_dropped = Signal(list)

    def __init__(
        self,
        caption: str,
        suffixes: Iterable[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self.suffixes = {s.lower() for s in (suffixes or [])}
        self.setMinimumHeight(84)
        layout = QVBoxLayout(self)
        self.label = QLabel(caption)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setObjectName("Subtitle")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt API
        """Accept a drag that carries at least one supported file."""
        if event.mimeData().hasUrls() and self._accepted(event.mimeData().urls()):
            event.acceptProposedAction()
            self.setStyleSheet("QFrame#Card { border: 2px dashed palette(highlight); }")
        else:
            event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        """Clear the highlight."""
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API
        """Emit the dropped paths."""
        self.setStyleSheet("")
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile() and self._supported(Path(url.toLocalFile()))
        ]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def _accepted(self, urls: list[Any]) -> bool:
        return any(url.isLocalFile() and self._supported(Path(url.toLocalFile())) for url in urls)

    def _supported(self, path: Path) -> bool:
        if path.is_dir():
            return True
        return not self.suffixes or path.suffix.lower() in self.suffixes


class ImageCanvas(QScrollArea):
    """A pan-and-zoom image viewer used for page previews."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self._label)
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._fit = True

    def load(self, path: Path | str) -> bool:
        """Show an image file; returns ``False`` when it cannot be read."""
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._label.setText(f"Cannot display {Path(path).name}")
            self._pixmap = None
            return False
        self._pixmap = pixmap
        self._fit = True
        self._render()
        return True

    def clear(self, message: str = "") -> None:
        """Remove the image."""
        self._pixmap = None
        self._label.setPixmap(QPixmap())
        self._label.setText(message)
        self._label.adjustSize()

    def set_zoom(self, zoom: float) -> None:
        """Set an explicit zoom factor."""
        self._zoom = max(0.05, min(8.0, zoom))
        self._fit = False
        self._render()

    def zoom_in(self) -> None:
        """Zoom in by 25%."""
        self.set_zoom(self._zoom * 1.25)

    def zoom_out(self) -> None:
        """Zoom out by 20%."""
        self.set_zoom(self._zoom * 0.8)

    def fit(self) -> None:
        """Scale the image to the viewport."""
        self._fit = True
        self._render()

    def zoom(self) -> float:
        """The current zoom factor."""
        return self._zoom

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        """Re-fit when the viewport changes size."""
        super().resizeEvent(event)
        if self._fit:
            self._render()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        """Ctrl+wheel zooms, plain wheel scrolls."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def _render(self) -> None:
        if self._pixmap is None:
            return
        if self._fit:
            viewport = self.viewport().size()
            if viewport.width() > 10 and viewport.height() > 10:
                scaled = self._pixmap.scaled(
                    viewport - QSize(4, 4),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._zoom = scaled.width() / max(1, self._pixmap.width())
            else:
                scaled = self._pixmap
        else:
            scaled = self._pixmap.scaled(
                self._pixmap.size() * self._zoom,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())


class DataTable(QTableWidget):
    """A read-mostly table with sensible defaults."""

    def __init__(self, headers: list[str], parent: QWidget | None = None) -> None:
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSortingEnabled(True)

    def fill(self, rows: list[list[Any]], *, user_data: list[Any] | None = None) -> None:
        """Replace the contents with *rows*."""
        self.setSortingEnabled(False)
        self.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QTableWidgetItem("" if value is None else str(value))
                if isinstance(value, int | float) and not isinstance(value, bool):
                    item.setData(Qt.ItemDataRole.EditRole, value)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if column == 0 and user_data is not None and row_index < len(user_data):
                    item.setData(Qt.ItemDataRole.UserRole, user_data[row_index])
                self.setItem(row_index, column, item)
        self.setSortingEnabled(True)
        self.resizeColumnsToContents()

    def selected_data(self) -> Any:
        """``UserRole`` payload of the selected row."""
        items = self.selectedItems()
        if not items:
            return None
        return self.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)


class Toolbar(QWidget):
    """A horizontal row of buttons with an optional stretch."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)

    def add(self, widget: QWidget) -> QWidget:
        """Append a widget."""
        self._layout.addWidget(widget)
        return widget

    def stretch(self) -> None:
        """Push the following widgets to the far side."""
        self._layout.addStretch(1)


def thumbnail(path: Path | str, size: int = 96) -> QPixmap:
    """Load a scaled thumbnail, or a neutral placeholder."""
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.darkGray)
        painter = QPainter(pixmap)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "?")
        painter.end()
        return pixmap
    return pixmap.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


def confirm(parent: QWidget, title: str, message: str) -> bool:
    """Ask the operator to confirm a destructive action."""
    from PySide6.QtWidgets import QMessageBox

    answer = QMessageBox.question(
        parent, title, message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def show_error(parent: QWidget, title: str, message: str, detail: str = "") -> None:
    """Report a failure without crashing the UI."""
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(message)
    if detail:
        box.setDetailedText(detail)
    box.exec()


def run_guarded(parent: QWidget, title: str, operation: Callable[[], Any]) -> Any:
    """Run *operation*, reporting any failure instead of propagating it."""
    try:
        return operation()
    except Exception as exc:  # noqa: BLE001 - the UI must never die on an action
        import traceback

        log.exception("%s failed", title)
        show_error(parent, title, str(exc)[:400], traceback.format_exc())
        return None
