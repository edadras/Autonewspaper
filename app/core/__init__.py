"""Core infrastructure: errors, events, jobs, undo, versioning, DI."""

from app.core.container import ServiceContainer
from app.core.errors import AppError, Component, ErrorReport, Severity, to_report
from app.core.events import Event, EventBus, EventType
from app.core.jobs import CancelToken, Job, JobContext, JobLane, JobQueue, JobState
from app.core.logging_setup import get_buffer, setup_logging
from app.core.undo import Command, UndoStack
from app.core.versioning import VersionInfo, VersionManager

__all__ = [
    "ServiceContainer",
    "AppError",
    "Component",
    "Severity",
    "ErrorReport",
    "to_report",
    "Event",
    "EventBus",
    "EventType",
    "Job",
    "JobQueue",
    "JobContext",
    "JobLane",
    "JobState",
    "CancelToken",
    "setup_logging",
    "get_buffer",
    "Command",
    "UndoStack",
    "VersionManager",
    "VersionInfo",
]
