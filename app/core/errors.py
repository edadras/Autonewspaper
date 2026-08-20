"""Error taxonomy and structured error reports.

Nothing in the application is allowed to crash the process. Every failure is
converted into an :class:`AppError` carrying enough context to be logged,
shown in the UI and - where possible - recovered from automatically.
"""

from __future__ import annotations

import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How badly a failure affects the running operation."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Component(str, Enum):
    """Sub-system a failure originated from."""

    CORE = "core"
    CONFIG = "config"
    DATABASE = "database"
    CONTENT = "content"
    ASSETS = "assets"
    AI = "ai"
    IMAGE_AI = "image_ai"
    LAYOUT = "layout"
    TEMPLATE = "template"
    INDESIGN = "indesign"
    PHOTOSHOP = "photoshop"
    AUTOMATION = "automation"
    VISION = "vision"
    EXPORT = "export"
    UI = "ui"
    PIPELINE = "pipeline"
    AGENT = "agent"


@dataclass
class ErrorReport:
    """Serialisable description of a single failure."""

    error_type: str
    message: str
    component: Component
    severity: Severity = Severity.ERROR
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    recovery_action: str | None = None
    stack_trace: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    error_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dictionary."""
        return {
            "error_id": self.error_id,
            "error_type": self.error_type,
            "message": self.message,
            "component": self.component.value,
            "severity": self.severity.value,
            "timestamp": self.timestamp.isoformat(),
            "recovery_action": self.recovery_action,
            "stack_trace": self.stack_trace,
            "context": self.context,
        }

    def summary(self) -> str:
        """One-line human readable summary."""
        return f"[{self.severity.value.upper()}][{self.component.value}] {self.error_type}: {self.message}"


class AppError(Exception):
    """Base class for every error raised by the application."""

    component: Component = Component.CORE
    severity: Severity = Severity.ERROR
    recovery_action: str | None = None

    def __init__(
        self,
        message: str,
        *,
        component: Component | None = None,
        severity: Severity | None = None,
        recovery_action: str | None = None,
        context: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if component is not None:
            self.component = component
        if severity is not None:
            self.severity = severity
        if recovery_action is not None:
            self.recovery_action = recovery_action
        self.context = context or {}
        self.__cause__ = cause

    def report(self) -> ErrorReport:
        """Build an :class:`ErrorReport` from this exception."""
        cause = self.__cause__
        stack = None
        if cause is not None:
            stack = "".join(traceback.format_exception(type(cause), cause, cause.__traceback__))
        elif self.__traceback__ is not None:
            stack = "".join(traceback.format_exception(type(self), self, self.__traceback__))
        return ErrorReport(
            error_type=type(self).__name__,
            message=self.message,
            component=self.component,
            severity=self.severity,
            recovery_action=self.recovery_action,
            stack_trace=stack,
            context=dict(self.context),
        )


class ConfigurationError(AppError):
    """Invalid or missing configuration."""

    component = Component.CONFIG
    recovery_action = "Open Settings and correct the highlighted value."


class DatabaseError(AppError):
    """Persistence failure."""

    component = Component.DATABASE
    recovery_action = "The project database may be locked; close other instances and retry."


class ContentImportError(AppError):
    """A source document could not be imported."""

    component = Component.CONTENT
    severity = Severity.WARNING
    recovery_action = "Check the file encoding/format, or paste the text manually."


class AssetError(AppError):
    """An image or other asset could not be used."""

    component = Component.ASSETS
    severity = Severity.WARNING


class AIProviderError(AppError):
    """An AI provider call failed."""

    component = Component.AI
    recovery_action = "Verify the API key and network access, or switch provider in AI Settings."


class AIResponseError(AIProviderError):
    """The provider replied with content the application cannot parse."""

    recovery_action = "Retrying with a stricter prompt; lower the temperature if this repeats."


class ImageGenerationError(AppError):
    """Image generation failed."""

    component = Component.IMAGE_AI
    severity = Severity.WARNING
    recovery_action = "Falling back to an existing asset or a generated placeholder frame."


class LayoutError(AppError):
    """The layout engine could not satisfy the constraints."""

    component = Component.LAYOUT
    recovery_action = "Reduce the number of articles per page or increase the page count."


class TemplateError(AppError):
    """A template is missing or malformed."""

    component = Component.TEMPLATE
    recovery_action = "Select a different template in Project Settings."


class AdobeNotFoundError(AppError):
    """No supported Adobe installation was detected."""

    component = Component.INDESIGN
    severity = Severity.CRITICAL
    recovery_action = "Install Adobe InDesign, or set its path manually in Adobe Settings."


class AdobeConnectionError(AppError):
    """Adobe is installed but could not be driven."""

    component = Component.INDESIGN
    recovery_action = "The controller will retry with the next automation strategy."


class ScriptExecutionError(AppError):
    """An ExtendScript/JSX call returned an error."""

    component = Component.INDESIGN
    recovery_action = "The failing step will be retried through the fallback automation layer."


class AutomationError(AppError):
    """Every automation strategy failed for an operation."""

    component = Component.AUTOMATION
    recovery_action = "Run System Diagnostics; the operation can be completed manually in InDesign."


class VisionQAError(AppError):
    """Quality assurance could not analyse a rendered page."""

    component = Component.VISION
    severity = Severity.WARNING
    recovery_action = "Geometric QA results are used instead of the vision model."


class ExportError(AppError):
    """Export of the final document failed."""

    component = Component.EXPORT
    recovery_action = "Check that the output directory is writable and not open in another program."


class PipelineAbort(AppError):
    """The pipeline was cancelled by the user."""

    component = Component.PIPELINE
    severity = Severity.INFO
    recovery_action = "The project state was saved and can be resumed."


class ToolValidationError(AppError):
    """An agent requested a tool call that failed validation."""

    component = Component.AGENT
    severity = Severity.WARNING
    recovery_action = "The agent is asked to correct its arguments and retry."


class PermissionDeniedError(AppError):
    """An agent tool call was denied by the permission policy."""

    component = Component.AGENT
    severity = Severity.WARNING
    recovery_action = "Grant the capability in Settings if the operation is expected."


def to_report(exc: BaseException, component: Component = Component.CORE) -> ErrorReport:
    """Convert *any* exception into an :class:`ErrorReport`."""
    if isinstance(exc, AppError):
        return exc.report()
    return ErrorReport(
        error_type=type(exc).__name__,
        message=str(exc) or type(exc).__name__,
        component=component,
        severity=Severity.ERROR,
        stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )
