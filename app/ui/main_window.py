"""The main window.

Owns the navigation, the page stack and the run controls. The pipeline runs on
a worker thread; approval requests are marshalled back onto the GUI thread and
the worker waits for the operator's answer.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.application import Application
from app.core.jobs import CancelToken
from app.models.schemas import ApprovalRequest, PipelineResult, PipelineStage
from app.services.project_manager import ProjectHandle
from app.ui.bridge import EventBridge
from app.ui.crash_guard import CrashGuard
from app.ui.pages.assets import AssetsPage
from app.ui.pages.base import Page
from app.ui.pages.content import ContentPage
from app.ui.pages.dashboard import DashboardPage
from app.ui.pages.export_page import ExportPage
from app.ui.pages.layout_page import LayoutPage
from app.ui.pages.logs import LogsPage
from app.ui.pages.preview import PreviewPage
from app.ui.pages.projects import NewProjectPage, ProjectsPage
from app.ui.pages.settings_pages import AdobeSettingsPage, AISettingsPage, DiagnosticsPage
from app.ui.pages.studio import StudioPage
from app.ui.pages.templates import TemplatesPage
from app.ui.widgets.common import confirm, show_error

log = logging.getLogger(__name__)


class PipelineWorker(QThread):
    """Runs the pipeline off the GUI thread."""

    finished_with = Signal(object)
    failed_with = Signal(str)

    def __init__(
        self,
        application: Application,
        handle: ProjectHandle,
        mode: str,
        token: CancelToken,
        approval: Any,
        parent: QWidget | None = None,
        resume_from: PipelineStage | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = application
        self.handle = handle
        self.mode = mode
        self.token = token
        self.approval = approval
        self.resume_from = resume_from

    def run(self) -> None:  # noqa: D102 - QThread API
        try:
            result = self.app.pipeline.run(
                self.handle,
                mode=self.mode,
                token=self.token,
                approval=self.approval,
                resume_from=self.resume_from,
            )
            self.finished_with.emit(result)
        except Exception as exc:  # noqa: BLE001 - never let a worker crash the app
            log.exception("Pipeline worker failed")
            self.failed_with.emit(str(exc))


class ApprovalDialog(QDialog):
    """Shown at the approval gates in semi-automatic mode."""

    def __init__(self, request: ApprovalRequest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(request.title)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel(request.title)
        heading.setObjectName("Title")
        layout.addWidget(heading)
        if request.description:
            description = QLabel(request.description)
            description.setWordWrap(True)
            description.setObjectName("Subtitle")
            layout.addWidget(description)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText(_format_payload(request.payload))
        detail.setMaximumHeight(320)
        layout.addWidget(detail)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Approve and continue")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Stop the run")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    """The application window."""

    approval_requested = Signal(object, object)

    def __init__(self, application: Application) -> None:
        super().__init__()
        self.app = application
        self.bridge = EventBridge(application.bus, self)
        # §32: an exception escaping a slot or a paint event must be reported,
        # not abort the process.
        self.crash_guard = CrashGuard(self).install()
        self.setWindowTitle("AI Newspaper Studio")
        self.resize(1440, 920)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setMaximumWidth(232)
        self.nav.setMinimumWidth(196)
        self.stack = QStackedWidget()

        self.pages: list[Page] = []
        for page_class in (
            DashboardPage,
            ProjectsPage,
            NewProjectPage,
            ContentPage,
            AssetsPage,
            TemplatesPage,
            LayoutPage,
            PreviewPage,
            ExportPage,
            StudioPage,
            AISettingsPage,
            AdobeSettingsPage,
            DiagnosticsPage,
            LogsPage,
        ):
            page = page_class(application, self.bridge, self)
            self.pages.append(page)
            self.stack.addWidget(page)
            item = QListWidgetItem(f"{page.icon}   {page.title}")
            item.setData(Qt.ItemDataRole.UserRole, page.title)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._navigate)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._build_menus()
        self._build_status_bar()

        self.worker: PipelineWorker | None = None
        self.token: CancelToken | None = None
        self.approval_requested.connect(self._on_approval, Qt.ConnectionType.QueuedConnection)
        self.bridge.error_raised.connect(self._on_error)
        self.bridge.job_changed.connect(self._on_job)

        self.nav.setCurrentRow(0)
        self._restore_last_project()

    # ------------------------------------------------------------- chrome
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        new_action = QAction("&New project", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(lambda: self.show_page("New Project"))
        file_menu.addAction(new_action)
        open_action = QAction("&Open project…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(lambda: self.show_page("Projects"))
        file_menu.addAction(open_action)
        save_action = QAction("&Save project", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._save_project)
        file_menu.addAction(save_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("&Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self._undo)
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction("&Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self._redo)
        edit_menu.addAction(self.redo_action)
        self.app.undo.subscribe(self._sync_undo_actions)

        run_menu = self.menuBar().addMenu("&Run")
        generate_action = QAction("&Generate Newspaper", self)
        generate_action.setShortcut("F5")
        generate_action.triggered.connect(lambda: self.start_generation())
        run_menu.addAction(generate_action)
        stop_action = QAction("&Stop", self)
        stop_action.setShortcut("Esc")
        stop_action.triggered.connect(self.cancel_generation)
        run_menu.addAction(stop_action)
        run_menu.addSeparator()
        version_action = QAction("Create a &version snapshot", self)
        version_action.triggered.connect(self._snapshot)
        run_menu.addAction(version_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        diagnostics_action = QAction("System &Diagnostics", self)
        diagnostics_action.triggered.connect(lambda: self.show_page("Diagnostics"))
        tools_menu.addAction(diagnostics_action)
        adobe_action = QAction("&Adobe Settings", self)
        adobe_action.triggered.connect(lambda: self.show_page("Adobe Settings"))
        tools_menu.addAction(adobe_action)
        ai_action = QAction("A&I Settings", self)
        ai_action.triggered.connect(lambda: self.show_page("AI Settings"))
        tools_menu.addAction(ai_action)

        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.project_label = QLabel("No project")
        self.job_label = QLabel("")
        self.job_label.setObjectName("Subtitle")
        self.busy = QProgressBar()
        self.busy.setMaximumWidth(180)
        self.busy.setRange(0, 100)
        self.busy.setValue(0)
        self.busy.setVisible(False)
        bar.addWidget(self.project_label)
        bar.addPermanentWidget(self.job_label)
        bar.addPermanentWidget(self.busy)

    # ---------------------------------------------------------- navigation
    def show_page(self, title: str) -> None:
        """Switch to the page with the given title."""
        for index in range(self.nav.count()):
            if self.nav.item(index).data(Qt.ItemDataRole.UserRole) == title:
                self.nav.setCurrentRow(index)
                return

    def _navigate(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        page = self.pages[row]
        try:
            page.refresh()
        except Exception as exc:  # noqa: BLE001
            log.exception("Refreshing '%s' failed", page.title)
            show_error(self, page.title, str(exc)[:300])

    def refresh_all(self) -> None:
        """Refresh the visible page."""
        self._navigate(self.nav.currentRow())

    # ------------------------------------------------------------ projects
    def open_project(self, slug_or_path: str | Path) -> ProjectHandle:
        """Open a project and refresh the interface."""
        handle = self.app.open_project(slug_or_path)
        self.project_opened(handle)
        return handle

    def project_opened(self, handle: ProjectHandle) -> None:
        """Update the window after a project was opened or created."""
        project = handle.project()
        self.project_label.setText(
            f"{project['name']}  ·  {project['page_count']} page(s)  ·  {project['template_id']}"
        )
        self.setWindowTitle(f"AI Newspaper Studio - {project['name']}")
        resumable = self.app.projects.resumable(handle)
        self.refresh_all()
        if resumable:
            self.statusBar().showMessage(
                f"An interrupted run can be resumed from '{resumable['stage']}'.", 12000
            )

    def _restore_last_project(self) -> None:
        slug = self.app.last_project()
        if not slug:
            return
        try:
            self.open_project(slug)
        except Exception as exc:  # noqa: BLE001 - a missing project is not fatal
            log.info("Could not reopen the last project '%s': %s", slug, exc)

    def _save_project(self) -> None:
        if self.app.current is None:
            return
        path = self.app.projects.save(self.app.current)
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def _snapshot(self) -> None:
        if self.app.current is None:
            return
        info = self.app.current.versions.create(label="Manual snapshot")
        self.statusBar().showMessage(f"Created {info.name}", 6000)

    # ------------------------------------------------------------- running
    def start_generation(self, mode: str | None = None, resume_from: PipelineStage | None = None) -> None:
        """Start (or resume) the pipeline on a worker thread."""
        if self.app.current is None:
            show_error(self, "Generate", "Open or create a project first.")
            return
        if self.worker is not None and self.worker.isRunning():
            show_error(self, "Generate", "A run is already in progress.")
            return
        mode = mode or self.app.settings.settings.pipeline.mode
        self.token = CancelToken()
        self.worker = PipelineWorker(
            self.app,
            self.app.current,
            mode,
            self.token,
            self._approval_callback,
            self,
            resume_from=resume_from,
        )
        self.worker.finished_with.connect(self._on_run_finished)
        self.worker.failed_with.connect(self._on_run_failed)
        self.busy.setVisible(True)
        self.busy.setValue(0)
        self._dashboard().set_running(True)
        self.bridge.pipeline_stage.connect(self._on_stage)
        self.worker.start()
        self.statusBar().showMessage(
            f"Resuming from '{resume_from.value}' in '{mode}' mode…"
            if resume_from
            else f"Generating in '{mode}' mode…"
        )

    def cancel_generation(self) -> None:
        """Ask the running pipeline to stop."""
        if self.token is not None:
            self.token.cancel()
            self.statusBar().showMessage("Stopping…", 4000)

    def _approval_callback(self, request: ApprovalRequest) -> bool:
        """Called from the worker thread; blocks until the operator answers."""
        event = threading.Event()
        answer: dict[str, bool] = {"approved": False}
        self.approval_requested.emit(request, (event, answer))
        event.wait(timeout=3600)
        return answer["approved"]

    def _on_approval(self, request: ApprovalRequest, payload: tuple) -> None:
        event, answer = payload
        try:
            dialog = ApprovalDialog(request, self)
            answer["approved"] = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            event.set()

    def _on_stage(self, stage: str, progress: float) -> None:
        self.busy.setValue(int(progress * 100))
        self.statusBar().showMessage(f"Stage: {stage.replace('_', ' ')}")

    def _on_run_finished(self, result: PipelineResult) -> None:
        self.busy.setVisible(False)
        self._dashboard().set_running(False)
        self.statusBar().showMessage(result.summary(), 15000)
        self.refresh_all()
        if result.success and result.pdf_paths:
            self.show_page("Preview")

    def _on_run_failed(self, message: str) -> None:
        self.busy.setVisible(False)
        self._dashboard().set_running(False)
        show_error(self, "Generation failed", message)

    def _dashboard(self) -> DashboardPage:
        return self.pages[0]  # type: ignore[return-value]

    # -------------------------------------------------------------- events
    def _on_error(self, payload: dict) -> None:
        message = payload.get("message") or str(payload)
        self.statusBar().showMessage(f"Error: {message[:160]}", 12000)

    def _on_job(self, payload: dict) -> None:
        job = payload.get("job") or {}
        active = len(self.app.jobs.active())
        if active:
            self.job_label.setText(f"{active} job(s) running - {job.get('name', '')[:48]}")
        else:
            self.job_label.setText("")

    # ---------------------------------------------------------------- undo
    def _undo(self) -> None:
        label = self.app.undo.undo()
        if label:
            self.statusBar().showMessage(f"Undone: {label}", 4000)
            self.refresh_all()

    def _redo(self) -> None:
        label = self.app.undo.redo()
        if label:
            self.statusBar().showMessage(f"Redone: {label}", 4000)
            self.refresh_all()

    def _sync_undo_actions(self) -> None:
        self.undo_action.setEnabled(self.app.undo.can_undo)
        self.redo_action.setEnabled(self.app.undo.can_redo)
        self.undo_action.setText(f"&Undo {self.app.undo.undo_label() or ''}".rstrip())
        self.redo_action.setText(f"&Redo {self.app.undo.redo_label() or ''}".rstrip())

    # --------------------------------------------------------------- about
    def _about(self) -> None:
        info = self.app.describe()
        QMessageBox.about(
            self,
            "About AI Newspaper Studio",
            f"<b>AI Newspaper Studio {info['version']}</b><br><br>"
            "Autonomous newspaper and magazine production driving Adobe InDesign "
            "and Photoshop through their scripting APIs.<br><br>"
            f"Data directory: {info['data_dir']}<br>"
            f"Templates: {info['templates']}<br>"
            f"AI: {info['ai']['text']['name']}/{info['ai']['text']['model']}<br>"
            f"InDesign: {info['adobe']['indesign']['version'] or 'not detected'}<br>"
            f"Photoshop: {info['adobe']['photoshop']['version'] or 'not detected'}",
        )

    # -------------------------------------------------------------- closing
    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        """Stop a running job before the window closes."""
        if self.worker is not None and self.worker.isRunning():
            if not confirm(self, "Quit", "A generation run is in progress. Stop it and quit?"):
                event.ignore()
                return
            self.cancel_generation()
            self.worker.wait(8000)
        for page in self.pages:
            page.tasks.wait_all(5000)
        try:
            self.bridge.close()
        except Exception:  # noqa: BLE001
            pass
        self.crash_guard.uninstall()
        event.accept()


def _format_payload(payload: dict[str, Any]) -> str:
    """Render an approval payload as readable text."""
    import json

    if not payload:
        return "(no details)"
    if "analyses" in payload:
        lines = ["Editorial plan:"]
        for item in payload["analyses"][:40]:
            lines.append(
                f"  page {item.get('recommended_page')} "
                f"[{item.get('recommended_area')}] "
                f"priority {item.get('importance')}  {item.get('headline', '')[:60]}"
            )
        assignments = payload.get("page_assignments") or {}
        lines.append("")
        lines.append(f"Page assignments: {assignments}")
        return "\n".join(lines)
    if "pages" in payload:
        lines = ["Pages:"]
        for page in payload["pages"]:
            lines.append(
                f"  page {page.get('index')}: score {page.get('score', 0):.1f}, "
                f"{page.get('elements', 0)} frame(s)"
            )
        return "\n".join(lines)
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)[:4000]
