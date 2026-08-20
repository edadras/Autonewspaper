"""Last-resort handler for exceptions that escape everything else.

Specification §32: no error may take the whole application down, and every
failure must be reported with its type, message, component, timestamp,
recovery action and stack trace. The pages already guard their own actions;
this covers what they cannot - an exception raised inside a Qt slot, a paint
event or a worker thread, where the default behaviour is a silent abort or a
traceback on a console the operator never sees.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from app.core.errors import Component, ErrorReport, to_report

log = logging.getLogger("app.unhandled")

#: The same fault repeating (a broken paint event fires on every repaint)
#: must not produce a dialog per occurrence.
REPEAT_WINDOW_SECONDS = 20.0


class CrashGuard(QObject):
    """Installs process-wide hooks that report instead of terminating."""

    reported = Signal(object)
    """Emitted with the :class:`ErrorReport` on the GUI thread."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._previous_excepthook: Any = None
        self._previous_threadhook: Any = None
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self.reported.connect(self._show)

    # ----------------------------------------------------------- lifecycle
    def install(self) -> CrashGuard:
        """Take over ``sys.excepthook`` and ``threading.excepthook``."""
        self._previous_excepthook = sys.excepthook
        self._previous_threadhook = threading.excepthook
        sys.excepthook = self._on_exception
        threading.excepthook = self._on_thread_exception  # type: ignore[assignment]
        return self

    def uninstall(self) -> None:
        """Restore the hooks that were in place before :meth:`install`."""
        if self._previous_excepthook is not None:
            sys.excepthook = self._previous_excepthook
            self._previous_excepthook = None
        if self._previous_threadhook is not None:
            threading.excepthook = self._previous_threadhook  # type: ignore[assignment]
            self._previous_threadhook = None

    # -------------------------------------------------------------- hooks
    def _on_exception(self, exc_type: type, exc_value: BaseException, exc_tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        if exc_value.__traceback__ is None:
            exc_value.__traceback__ = exc_tb
        self.handle(exc_value, Component.UI)

    def _on_thread_exception(self, args: Any) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        exception = args.exc_value or args.exc_type("unknown error in a worker thread")
        if exception.__traceback__ is None:
            exception.__traceback__ = args.exc_traceback
        thread = getattr(args.thread, "name", "?")
        # A worker thread must not open a modal dialog, so it is logged only;
        # the run that owns the thread reports its own failure to the user.
        report = to_report(exception, Component.CORE)
        report.context.setdefault("thread", thread)
        self._log(report)

    # ------------------------------------------------------------ reporting
    def handle(self, exc: BaseException, component: Component = Component.UI) -> ErrorReport:
        """Log *exc* and, unless it is a repeat, show it to the operator."""
        report = to_report(exc, component)
        self._log(report)
        if self._is_repeat(report):
            log.debug("Suppressed a repeat of %s", report.error_type)
            return report
        # Queued through the signal so a fault raised on a worker thread never
        # builds a widget anywhere but the GUI thread.
        self.reported.emit(report)
        return report

    def _log(self, report: ErrorReport) -> None:
        log.critical("%s", report.summary(), extra={"error_report": report.to_dict()})
        if report.stack_trace:
            log.debug("%s\n%s", report.error_id, report.stack_trace)

    def _is_repeat(self, report: ErrorReport) -> bool:
        signature = f"{report.error_type}:{report.message[:120]}"
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(signature)
            self._seen[signature] = now
            if len(self._seen) > 64:
                cutoff = now - REPEAT_WINDOW_SECONDS
                self._seen = {key: at for key, at in self._seen.items() if at >= cutoff}
        return last is not None and now - last < REPEAT_WINDOW_SECONDS

    def _show(self, report: ErrorReport) -> None:
        """Show the report in a dialog, on the GUI thread."""
        from app.ui.widgets.common import show_error

        parent = self.parent()
        widget = parent if hasattr(parent, "window") else None
        detail = "\n".join(
            part
            for part in (
                f"Error ID: {report.error_id}",
                f"Component: {report.component.value}",
                f"Time: {report.timestamp.isoformat()}",
                f"Recovery: {report.recovery_action}" if report.recovery_action else "",
                "",
                report.stack_trace or "",
            )
            if part != ""
        )
        message = report.message
        if report.recovery_action:
            message = f"{message}\n\n{report.recovery_action}"
        # Deferred so the dialog is not opened from inside the failing call.
        QTimer.singleShot(
            0, lambda: show_error(widget, f"Unexpected error ({report.error_type})", message, detail)
        )
