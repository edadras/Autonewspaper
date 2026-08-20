"""Logging configuration.

Two sinks are always installed: a rotating file handler under the user data
directory and an in-memory ring buffer that feeds the Logs page of the UI.
Per-project logging can be attached and detached at runtime.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-38s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class LogRecordBuffer(logging.Handler):
    """Thread-safe ring buffer of recent log records for the UI."""

    def __init__(self, capacity: int = 5000) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - logging API
        try:
            item = {
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "timestamp": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "exc": self.format(record) if record.exc_info else None,
            }
        except Exception:  # pragma: no cover - never break logging
            return
        with self._lock:
            self._records.append(item)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(item)
            except Exception:  # pragma: no cover
                pass

    def records(self, level: str | None = None, contains: str | None = None) -> list[dict[str, Any]]:
        """Snapshot of the buffer, optionally filtered."""
        with self._lock:
            items = list(self._records)
        if level:
            items = [r for r in items if r["level"] == level]
        if contains:
            needle = contains.lower()
            items = [r for r in items if needle in r["message"].lower() or needle in r["logger"].lower()]
        return items

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Register *listener*; returns a callable that unsubscribes."""
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsubscribe

    def clear(self) -> None:
        """Drop every buffered record."""
        with self._lock:
            self._records.clear()


_BUFFER = LogRecordBuffer()
_CONFIGURED = False


def get_buffer() -> LogRecordBuffer:
    """Return the process-wide log ring buffer."""
    return _BUFFER


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    *,
    console: bool = True,
    filename: str = "ai_newspaper_studio.log",
    max_bytes: int = 8 * 1024 * 1024,
    backups: int = 10,
) -> Path:
    """Install the root logging configuration and return the log file path."""
    global _CONFIGURED
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / filename

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    for handler in list(root.handlers):
        if getattr(handler, "_ains", False):
            root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    file_handler._ains = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    _BUFFER.setFormatter(formatter)
    _BUFFER.setLevel(logging.DEBUG)
    _BUFFER._ains = True  # type: ignore[attr-defined]
    if _BUFFER not in root.handlers:
        root.addHandler(_BUFFER)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(getattr(logging, level, logging.INFO))
        stream._ains = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    for noisy in ("httpx", "httpcore", "PIL", "urllib3", "comtypes", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_excepthooks()
    _CONFIGURED = True
    logging.getLogger(__name__).info("Logging initialised -> %s (level=%s)", log_file, level)
    return log_file


def is_configured() -> bool:
    """Whether :func:`setup_logging` has already run."""
    return _CONFIGURED


def attach_project_log(log_dir: Path, name: str = "project.log") -> logging.Handler:
    """Add a per-project file handler; returns the handler for later removal."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / name, maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    return handler


def detach_handler(handler: logging.Handler) -> None:
    """Remove and close a handler previously attached."""
    root = logging.getLogger()
    if handler in root.handlers:
        root.removeHandler(handler)
    try:
        handler.close()
    except Exception:  # pragma: no cover
        pass


def _install_excepthooks() -> None:
    """Route uncaught exceptions (main thread and worker threads) to the log."""
    log = logging.getLogger("app.unhandled")

    def _hook(exc_type, exc_value, exc_tb) -> None:  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _hook

    def _thread_hook(args: Any) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "Unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook  # type: ignore[assignment]


def dump_records(records: Iterable[dict[str, Any]], target: Path) -> Path:
    """Write buffered records to *target* as plain text (used by bug reports)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[{r['time']}] {r['level']:<8} {r['logger']}: {r['message']}" for r in records]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
