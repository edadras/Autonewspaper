"""Dashboard: the state of the current edition and the one-click generate."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)

from app.models.schemas import PipelineStage
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, MetricTile, StatusPill, Toolbar

log = logging.getLogger(__name__)


class DashboardPage(Page):
    """Overview, run controls and live progress."""

    title = "Dashboard"
    subtitle = "The state of the current edition and the one-click generation run."
    icon = "▣"

    def build(self) -> None:
        """Create the metrics, run controls and progress panel."""
        self.metrics: dict[str, MetricTile] = {}
        grid = QGridLayout()
        grid.setSpacing(12)
        for column, (key, caption) in enumerate(
            [
                ("articles", "Articles"),
                ("words", "Words"),
                ("assets", "Images"),
                ("pages", "Pages"),
                ("average_score", "QA score"),
            ]
        ):
            tile = MetricTile(caption)
            self.metrics[key] = tile
            grid.addWidget(tile, 0, column)
        self.root.addLayout(grid)

        run_card = Card("Generate")
        controls = Toolbar()
        self.mode_box = QComboBox()
        self.mode_box.addItems(["auto", "semi_auto", "manual"])
        self.mode_box.setCurrentText(self.app.settings.settings.pipeline.mode)
        self.mode_box.currentTextChanged.connect(self._mode_changed)
        controls.add(QLabel("Mode:"))
        controls.add(self.mode_box)
        controls.stretch()
        self.generate_button = QPushButton("🚀  Generate Newspaper")
        self.generate_button.setObjectName("Primary")
        self.generate_button.clicked.connect(self._generate)
        self.cancel_button = QPushButton("Stop")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        controls.add(self.generate_button)
        controls.add(self.cancel_button)
        run_card.add(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%  -  idle")
        run_card.add(self.progress)

        self.stage_row = QWidget()
        stage_layout = QHBoxLayout(self.stage_row)
        stage_layout.setContentsMargins(0, 4, 0, 0)
        stage_layout.setSpacing(4)
        self.stage_pills: dict[str, StatusPill] = {}
        for stage in PipelineStage.ordered()[:-1]:
            pill = StatusPill(stage.value.replace("_", " "), "#666")
            self.stage_pills[stage.value] = pill
            stage_layout.addWidget(pill)
        stage_layout.addStretch(1)
        run_card.add(self.stage_row)
        self.root.addWidget(run_card)

        status_card = Card("Last run")
        self.status_label = QLabel("No run yet.")
        self.status_label.setWordWrap(True)
        status_card.add(self.status_label)
        self.warnings_label = QLabel("")
        self.warnings_label.setObjectName("Subtitle")
        self.warnings_label.setWordWrap(True)
        status_card.add(self.warnings_label)
        self.root.addWidget(status_card)

        environment = Card("Environment")
        self.environment_label = QLabel("")
        self.environment_label.setWordWrap(True)
        self.environment_label.setTextFormat(Qt.TextFormat.RichText)
        environment.add(self.environment_label)
        self.root.addWidget(environment)
        self.root.addStretch(1)

        self.bridge.pipeline_stage.connect(self._on_stage)
        self.bridge.pipeline_finished.connect(self._on_finished)
        self.bridge.pipeline_failed.connect(self._on_failed)
        self.bridge.pipeline_cancelled.connect(self._on_cancelled)
        self._token = None

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        """Reload the metrics and the environment summary."""
        handle = self.handle
        self.generate_button.setEnabled(handle is not None)
        if handle is None:
            for tile in self.metrics.values():
                tile.set_value("-")
            self.status_label.setText("Open or create a project to begin.")
            self.environment_label.setText(self._environment_html())
            return
        stats = self.app.projects.statistics(handle)
        for key, tile in self.metrics.items():
            tile.set_value(stats.get(key, 0))
        last = stats.get("last_run")
        if last:
            self.status_label.setText(
                f"Status: {last['status']} - stage '{last['stage']}' - score {last['score']:.1f}"
            )
        else:
            self.status_label.setText("No run yet for this project.")
        resumable = self.app.projects.resumable(handle)
        if resumable:
            self.status_label.setText(
                self.status_label.text()
                + f"\nAn interrupted run is available to resume from '{resumable['stage']}'."
            )
        self.environment_label.setText(self._environment_html())

    def _environment_html(self) -> str:
        adobe = self.app.adobe
        ai = self.app.settings.settings.ai
        indesign = (
            f"InDesign {adobe.indesign_app.version or ''}".strip()
            if adobe.indesign_app.installed
            else "InDesign not detected - the built-in renderer will produce previews and the PDF"
        )
        photoshop = (
            f"Photoshop {adobe.photoshop_app.version or ''}".strip()
            if adobe.photoshop_app.installed
            else "Photoshop not detected - images are processed locally"
        )
        return (
            f"<b>AI</b>: {ai.provider}/{ai.model} &nbsp;|&nbsp; "
            f"<b>Vision</b>: {ai.vision_provider}/{ai.vision_model} &nbsp;|&nbsp; "
            f"<b>Images</b>: {self.app.settings.settings.image_ai.provider}<br>"
            f"<b>Adobe</b>: {indesign} &nbsp;|&nbsp; {photoshop}"
        )

    # -------------------------------------------------------------- actions
    def _mode_changed(self, mode: str) -> None:
        self.app.settings.set_path("pipeline.mode", mode)

    def _generate(self) -> None:
        if self.handle is None:
            return
        window = self.window()
        starter = getattr(window, "start_generation", None)
        if callable(starter):
            starter(self.mode_box.currentText())

    def _cancel(self) -> None:
        window = self.window()
        stopper = getattr(window, "cancel_generation", None)
        if callable(stopper):
            stopper()

    # --------------------------------------------------------------- events
    def set_running(self, running: bool) -> None:
        """Toggle the run controls."""
        self.generate_button.setEnabled(not running and self.handle is not None)
        self.cancel_button.setEnabled(running)
        if running:
            for pill in self.stage_pills.values():
                pill.set_status(pill.text(), "#666")
            self.progress.setValue(0)

    def _on_stage(self, stage: str, progress: float) -> None:
        self.progress.setValue(int(progress * 100))
        self.progress.setFormat(f"%p%  -  {stage.replace('_', ' ')}")
        reached = False
        for name, pill in self.stage_pills.items():
            if name == stage:
                pill.set_status(pill.text(), "#3f8cff")
                reached = True
            elif not reached:
                pill.set_status(pill.text(), "#3fbf7f")

    def _on_finished(self, payload: dict[str, Any]) -> None:
        result = payload.get("result") or {}
        self.progress.setValue(100)
        self.progress.setFormat("%p%  -  finished")
        for pill in self.stage_pills.values():
            pill.set_status(pill.text(), "#3fbf7f")
        pdfs = result.get("pdf_paths") or []
        self.status_label.setText(
            f"Completed in {result.get('duration_seconds', 0):.1f}s - score "
            f"{result.get('score', 0):.1f} - {len(pdfs)} PDF(s) - "
            f"engine: {result.get('adobe_strategy', 'unknown')}"
        )
        warnings = result.get("warnings") or []
        self.warnings_label.setText("\n".join(f"• {w}" for w in warnings[:6]) if warnings else "")
        self.set_running(False)
        self.refresh()

    def _on_failed(self, payload: dict[str, Any]) -> None:
        error = payload.get("error") or {}
        self.progress.setFormat("%p%  -  failed")
        self.status_label.setText(f"Run failed: {error.get('message', 'unknown error')}")
        self.set_running(False)

    def _on_cancelled(self, payload: dict[str, Any]) -> None:
        self.progress.setFormat("%p%  -  cancelled")
        self.status_label.setText(f"Run cancelled: {payload.get('reason', '')}")
        self.set_running(False)
