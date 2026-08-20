"""Composition root.

Builds every service once, wires them through the dependency-injection
container and exposes the result as :class:`Application`. The UI, the CLI and
the tests all start from here, so there is exactly one place where the object
graph is defined.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.adobe.service import AdobeService
from app.ai.prompts import PromptLibrary
from app.ai.registry import AIService
from app.config.paths import AppPaths, get_paths
from app.config.settings import SettingsManager
from app.core.container import ServiceContainer
from app.core.events import EventBus
from app.core.jobs import JobQueue
from app.core.logging_setup import setup_logging
from app.core.undo import UndoStack
from app.database.session import AppDatabase
from app.export.exporter import ExportService
from app.services.asset_manager import AssetManager
from app.services.content_manager import ContentManager
from app.services.diagnostics import DiagnosticsService
from app.services.pipeline import Pipeline
from app.services.project_manager import ProjectHandle, ProjectManager
from app.templates.manager import TemplateManager

log = logging.getLogger(__name__)


class Application:
    """The wired application.

    Parameters
    ----------
    data_dir:
        Override the user data directory (used by tests and portable installs).
    configure_logging:
        Install the logging handlers. The UI does this; tests usually do not.
    """

    def __init__(self, data_dir: Path | None = None, *, configure_logging: bool = True) -> None:
        self.paths: AppPaths = AppPaths.resolve(data_dir).ensure() if data_dir else get_paths()
        self.settings = SettingsManager(self.paths)
        if configure_logging:
            setup_logging(self.paths.logs, self.settings.settings.log_level)

        self.container = ServiceContainer()
        self.bus = EventBus()
        self.undo = UndoStack(self.settings.settings.ui.undo_steps)
        self.jobs = JobQueue(self.bus, self.settings.settings.pipeline.parallel_workers)
        self.app_db = AppDatabase(self.paths.data / "app.sqlite")
        self.prompts = PromptLibrary(self.paths.prompts)

        self.templates = TemplateManager(self.paths, self.app_db)
        self.templates.discover()
        self.ai = AIService(self.settings, self.prompts)
        self.adobe = AdobeService(self.settings, self.paths.cache, self.bus)
        self.projects = ProjectManager(self.paths, self.app_db, self.bus)
        self.content = ContentManager(self.bus)
        self.assets = AssetManager(self.ai, None, self.jobs, self.bus)
        self.exporter = ExportService(None, self.bus)
        self.diagnostics = DiagnosticsService(
            self.paths, self.settings, ai=self.ai, adobe=self.adobe, templates=self.templates
        )
        self.pipeline = Pipeline(
            self.settings,
            self.projects,
            self.templates,
            self.assets,
            self.ai,
            self.adobe,
            self.exporter,
            self.jobs,
            self.bus,
        )

        self._register()
        self.current: ProjectHandle | None = None
        log.info("Application ready (data directory: %s)", self.paths.data)

    def _register(self) -> None:
        """Publish every service in the container."""
        self.container.register(SettingsManager, self.settings)
        self.container.register(EventBus, self.bus)
        self.container.register(JobQueue, self.jobs)
        self.container.register(UndoStack, self.undo)
        self.container.register(AppDatabase, self.app_db)
        self.container.register(PromptLibrary, self.prompts)
        self.container.register(TemplateManager, self.templates)
        self.container.register(AIService, self.ai)
        self.container.register(AdobeService, self.adobe)
        self.container.register(ProjectManager, self.projects)
        self.container.register(ContentManager, self.content)
        self.container.register(AssetManager, self.assets)
        self.container.register(ExportService, self.exporter)
        self.container.register(DiagnosticsService, self.diagnostics)
        self.container.register(Pipeline, self.pipeline)
        self.container.register("app", self)

    # ------------------------------------------------------------ projects
    def open_project(self, slug_or_path: str | Path) -> ProjectHandle:
        """Open a project and make it current."""
        handle = self.projects.open(slug_or_path)
        self._bind_project(handle)
        return handle

    def create_project(self, spec: Any) -> ProjectHandle:
        """Create a project and make it current."""
        handle = self.projects.create(spec)
        self._bind_project(handle)
        return handle

    def _bind_project(self, handle: ProjectHandle) -> None:
        """Point the per-project services at *handle*."""
        self.current = handle
        self.undo.clear()
        self.assets.photoshop = self.adobe.photoshop
        self.exporter.indesign = self.adobe.indesign if self.adobe.indesign_app.installed else None
        self.projects.mark_interrupted_runs(handle)
        self.app_db.set_setting("last_project", handle.slug)

    def last_project(self) -> str:
        """Slug of the project opened last (for "resume on start")."""
        return self.app_db.get_setting("last_project", "")

    def reload_ai(self) -> None:
        """Rebuild the AI providers after a settings change."""
        self.ai.reload()

    def reload_adobe(self) -> None:
        """Re-run Adobe detection after a settings change."""
        self.adobe.redetect()
        if self.current is not None:
            self._bind_project(self.current)

    def describe(self) -> dict[str, Any]:
        """Summary used by the Dashboard and the About box."""
        from app import __version__

        return {
            "version": __version__,
            "data_dir": str(self.paths.data),
            "settings_file": str(self.settings.file),
            "templates": len(self.templates.ids()),
            "ai": self.ai.describe(),
            "adobe": self.adobe.describe(),
            "current_project": self.current.slug if self.current else None,
        }

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """Stop the workers and release every resource."""
        log.info("Shutting down")
        self.jobs.shutdown()
        try:
            self.ai.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.adobe.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.projects.close_all()
        self.app_db.dispose()
        self.container.dispose()


def create_application(data_dir: Path | None = None, *, configure_logging: bool = True) -> Application:
    """Build the application object graph."""
    return Application(data_dir, configure_logging=configure_logging)
