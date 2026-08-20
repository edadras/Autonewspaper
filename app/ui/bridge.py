"""Bridge between the head-less core and the Qt event loop.

The core publishes events from worker threads. Qt widgets may only be touched
from the GUI thread, so every event is re-emitted here as a queued Qt signal.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.core.events import Event, EventBus, EventType
from app.core.logging_setup import get_buffer

log = logging.getLogger(__name__)


class EventBridge(QObject):
    """Re-emits core events as Qt signals on the GUI thread."""

    any_event = Signal(object)
    pipeline_started = Signal(dict)
    pipeline_stage = Signal(str, float)
    pipeline_finished = Signal(dict)
    pipeline_failed = Signal(dict)
    pipeline_cancelled = Signal(dict)
    approval_required = Signal(dict)

    job_changed = Signal(dict)
    project_changed = Signal(dict)
    content_imported = Signal(dict)
    asset_changed = Signal(dict)
    layout_planned = Signal(dict)
    qa_report = Signal(dict)
    preview_ready = Signal(dict)
    export_ready = Signal(dict)
    error_raised = Signal(dict)
    log_line = Signal(dict)

    def __init__(self, bus: EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bus = bus
        self._unsubscribe = bus.subscribe(None, self._on_event)
        self._unsubscribe_log = get_buffer().subscribe(self._on_log)

    def _on_event(self, event: Event) -> None:
        """Called from any thread; only emits signals (which Qt queues)."""
        try:
            payload = dict(event.payload)
            payload["_type"] = event.type.value
            self.any_event.emit(payload)
            self._route(event.type, payload)
        except Exception:  # pragma: no cover - the bridge must never raise
            log.exception("Event bridge failed for %s", event.type)

    def _route(self, event_type: EventType, payload: dict[str, Any]) -> None:
        if event_type is EventType.PIPELINE_STARTED:
            self.pipeline_started.emit(payload)
        elif event_type is EventType.PIPELINE_STAGE:
            self.pipeline_stage.emit(str(payload.get("stage", "")), float(payload.get("progress", 0.0)))
        elif event_type is EventType.PIPELINE_FINISHED:
            self.pipeline_finished.emit(payload)
        elif event_type is EventType.PIPELINE_FAILED:
            self.pipeline_failed.emit(payload)
        elif event_type is EventType.PIPELINE_CANCELLED:
            self.pipeline_cancelled.emit(payload)
        elif event_type is EventType.APPROVAL_REQUIRED:
            self.approval_required.emit(payload)
        elif event_type in (
            EventType.JOB_QUEUED,
            EventType.JOB_STARTED,
            EventType.JOB_PROGRESS,
            EventType.JOB_FINISHED,
            EventType.JOB_FAILED,
        ):
            self.job_changed.emit(payload)
        elif event_type in (
            EventType.PROJECT_CREATED,
            EventType.PROJECT_OPENED,
            EventType.PROJECT_SAVED,
            EventType.PROJECT_CHANGED,
        ):
            self.project_changed.emit(payload)
        elif event_type is EventType.CONTENT_IMPORTED:
            self.content_imported.emit(payload)
        elif event_type in (EventType.ASSET_IMPORTED, EventType.ASSET_GENERATED):
            self.asset_changed.emit(payload)
        elif event_type in (EventType.LAYOUT_PLANNED, EventType.LAYOUT_SCORED, EventType.LAYOUT_CORRECTED):
            self.layout_planned.emit(payload)
        elif event_type is EventType.QA_REPORT:
            self.qa_report.emit(payload)
        elif event_type is EventType.PREVIEW_READY:
            self.preview_ready.emit(payload)
        elif event_type is EventType.EXPORT_READY:
            self.export_ready.emit(payload)
        elif event_type is EventType.ERROR:
            self.error_raised.emit(payload)

    def _on_log(self, record: dict[str, Any]) -> None:
        try:
            self.log_line.emit(record)
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        """Stop listening."""
        self._unsubscribe()
        self._unsubscribe_log()
