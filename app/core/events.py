"""A tiny thread-safe publish/subscribe bus.

The bus is the only communication channel between the head-less core (which
knows nothing about Qt) and the UI layer. The UI subscribes and marshals the
callbacks onto the Qt event loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class EventType(str, Enum):
    """Every event the core can emit."""

    PIPELINE_STARTED = "pipeline.started"
    PIPELINE_STAGE = "pipeline.stage"
    PIPELINE_PROGRESS = "pipeline.progress"
    PIPELINE_FINISHED = "pipeline.finished"
    PIPELINE_FAILED = "pipeline.failed"
    PIPELINE_CANCELLED = "pipeline.cancelled"
    APPROVAL_REQUIRED = "pipeline.approval_required"

    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_PROGRESS = "job.progress"
    JOB_FINISHED = "job.finished"
    JOB_FAILED = "job.failed"

    PROJECT_CREATED = "project.created"
    PROJECT_OPENED = "project.opened"
    PROJECT_SAVED = "project.saved"
    PROJECT_CHANGED = "project.changed"

    CONTENT_IMPORTED = "content.imported"
    ASSET_IMPORTED = "asset.imported"
    ASSET_GENERATED = "asset.generated"

    LAYOUT_PLANNED = "layout.planned"
    LAYOUT_SCORED = "layout.scored"
    LAYOUT_CORRECTED = "layout.corrected"

    ADOBE_CONNECTED = "adobe.connected"
    ADOBE_COMMAND = "adobe.command"
    ADOBE_DISCONNECTED = "adobe.disconnected"

    PREVIEW_READY = "preview.ready"
    QA_REPORT = "qa.report"
    EXPORT_READY = "export.ready"

    ERROR = "error"
    NOTICE = "notice"


@dataclass
class Event:
    """An immutable event carrying an arbitrary payload."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "core"

    def get(self, key: str, default: Any = None) -> Any:
        """Convenience accessor for ``payload``."""
        return self.payload.get(key, default)


Listener = Callable[[Event], None]


class EventBus:
    """Synchronous, thread-safe pub/sub bus.

    Listeners must be cheap and must never raise; exceptions are caught and
    logged so that a faulty subscriber can never break a pipeline.
    """

    def __init__(self) -> None:
        self._listeners: dict[EventType | None, list[Listener]] = {}
        self._lock = threading.RLock()

    def subscribe(self, event_type: EventType | None, listener: Listener) -> Callable[[], None]:
        """Subscribe to *event_type* (``None`` subscribes to everything)."""
        with self._lock:
            self._listeners.setdefault(event_type, []).append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                bucket = self._listeners.get(event_type, [])
                if listener in bucket:
                    bucket.remove(listener)

        return _unsubscribe

    def publish(self, event_type: EventType, /, **payload: Any) -> Event:
        """Publish an event built from *event_type* and keyword payload."""
        event = Event(type=event_type, payload=payload)
        self.emit(event)
        return event

    def emit(self, event: Event) -> None:
        """Deliver an already-built :class:`Event` to all matching listeners."""
        with self._lock:
            listeners = list(self._listeners.get(event.type, ())) + list(self._listeners.get(None, ()))
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # pragma: no cover - listeners must never break the core
                log.exception("Event listener failed for %s", event.type.value)

    def clear(self) -> None:
        """Remove every listener (used in tests)."""
        with self._lock:
            self._listeners.clear()
