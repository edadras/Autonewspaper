"""Running long work without freezing the interface.

Specification §29: no operation may block the window. Imports, analysis,
generation, export and the deep diagnostics all take seconds to minutes, so
they run on a worker thread and report back through Qt signals.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)


class BackgroundTask(QThread):
    """Runs one callable off the GUI thread.

    The callable receives an optional progress reporter and returns any value;
    the result is delivered on the GUI thread through :attr:`succeeded`, and a
    failure through :attr:`failed` with its traceback rather than as an
    exception that would reach the event loop.
    """

    succeeded = Signal(object)
    failed = Signal(str, str)
    progressed = Signal(str)

    def __init__(
        self,
        operation: Callable[..., Any],
        *,
        title: str = "",
        wants_progress: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.operation = operation
        self.title = title or getattr(operation, "__name__", "operation")
        self.wants_progress = wants_progress

    def run(self) -> None:  # noqa: D102 - QThread API
        try:
            if self.wants_progress:
                result = self.operation(self.progressed.emit)
            else:
                result = self.operation()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into Qt
            log.exception("%s failed", self.title)
            self.failed.emit(str(exc), traceback.format_exc())
            return
        self.succeeded.emit(result)


class TaskRunner(QObject):
    """Keeps running tasks alive and prevents overlapping runs of the same kind."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: dict[str, BackgroundTask] = {}

    def busy(self, key: str) -> bool:
        """Whether a task with this key is still running."""
        task = self._tasks.get(key)
        return bool(task and task.isRunning())

    def start(
        self,
        key: str,
        operation: Callable[..., Any],
        *,
        title: str = "",
        wants_progress: bool = False,
        on_success: Callable[[Any], None] | None = None,
        on_failure: Callable[[str, str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> BackgroundTask | None:
        """Start *operation*; returns ``None`` when that key is already busy."""
        if self.busy(key):
            log.info("'%s' is already running; the request was ignored", key)
            return None
        task = BackgroundTask(operation, title=title or key, wants_progress=wants_progress, parent=self)
        if on_success is not None:
            task.succeeded.connect(on_success)
        if on_failure is not None:
            task.failed.connect(on_failure)
        if on_progress is not None:
            task.progressed.connect(on_progress)
        if on_finished is not None:
            task.finished.connect(on_finished)
        task.finished.connect(lambda: self._tasks.pop(key, None))
        self._tasks[key] = task
        task.start()
        return task

    def wait_all(self, timeout_ms: int = 30_000) -> None:
        """Block until every running task finishes (used when closing)."""
        for task in list(self._tasks.values()):
            if task.isRunning():
                task.wait(timeout_ms)
