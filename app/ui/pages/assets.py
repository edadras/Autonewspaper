"""Assets page: import, inspect, assign and generate pictures."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.services.asset_manager import IMAGE_SUFFIXES
from app.ui.pages.base import Page
from app.ui.widgets.common import (
    Card,
    DataTable,
    DropArea,
    ImageCanvas,
    Toolbar,
    run_guarded,
    show_error,
)

log = logging.getLogger(__name__)


class AssetsPage(Page):
    """Picture library with measurements and assignment."""

    title = "Assets"
    subtitle = "Every picture is measured before it is used: resolution, sharpness, exposure, faces and duplicates."
    icon = "▨"

    def build(self) -> None:
        """Create the asset table, the preview and the controls."""
        toolbar = Toolbar()
        self.import_button = toolbar.add(QPushButton("Import images…"))
        self.import_button.clicked.connect(self._import)
        self.reanalyze_button = toolbar.add(QPushButton("Re-analyse"))
        self.reanalyze_button.clicked.connect(self._reanalyze)
        self.assign_box = QComboBox()
        self.assign_box.setMinimumWidth(220)
        toolbar.add(QLabel("Assign to:"))
        toolbar.add(self.assign_box)
        self.assign_button = toolbar.add(QPushButton("Assign"))
        self.assign_button.clicked.connect(self._assign)
        toolbar.stretch()
        self.auto_button = toolbar.add(QPushButton("Auto-assign"))
        self.auto_button.clicked.connect(self._auto_assign)
        self.generate_button = toolbar.add(QPushButton("Generate missing"))
        self.generate_button.setObjectName("Primary")
        self.generate_button.clicked.connect(self._generate)
        self.root.addWidget(toolbar)

        self.drop = DropArea("Drop images here  (JPG, PNG, WEBP, TIFF, PSD)", IMAGE_SUFFIXES)
        self.drop.files_dropped.connect(self._import_paths)
        self.root.addWidget(self.drop)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = DataTable(
            ["File", "Size", "Ratio", "Quality", "Sharp", "Faces", "Story", "Source", "Flags"]
        )
        self.table.itemSelectionChanged.connect(self._show_selected)
        splitter.addWidget(self.table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = ImageCanvas()
        self.canvas.setMinimumWidth(280)
        right_layout.addWidget(self.canvas, 1)
        detail_card = Card("Measurements")
        self.detail = QListWidget()
        self.detail.setMaximumHeight(190)
        detail_card.add(self.detail)
        right_layout.addWidget(detail_card)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.root.addWidget(self.status)
        self.bridge.asset_changed.connect(lambda _p: self.refresh())

    # ------------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Reload the asset list and the story picker."""
        handle = self.handle
        for widget in (self.import_button, self.drop, self.generate_button, self.auto_button):
            widget.setEnabled(handle is not None)
        if handle is None:
            self.table.fill([])
            self.assign_box.clear()
            self.status.setText("Open a project to manage its pictures.")
            return

        with handle.uow() as uow:
            assets = uow.assets.for_project(handle.project_id)
            titles = {
                a.id: a.display_title[:50] for a in uow.articles.for_project(handle.project_id)
            }
        rows = []
        for asset in assets:
            flags = []
            if asset.ai_generated:
                flags.append("AI")
            if asset.processed:
                flags.append("processed")
            if asset.duplicate_of:
                flags.append(f"duplicate of #{asset.duplicate_of}")
            if asset.quality_score < 45:
                flags.append("low quality")
            rows.append(
                [
                    asset.filename,
                    f"{asset.width}×{asset.height}",
                    round(asset.aspect_ratio, 2),
                    round(asset.quality_score, 1),
                    round(asset.blur_score, 1),
                    asset.face_count,
                    titles.get(asset.article_id or -1, "-"),
                    asset.source,
                    ", ".join(flags),
                ]
            )
        self.table.fill(rows, user_data=[a.id for a in assets])

        self.assign_box.clear()
        self.assign_box.addItem("(unassigned)", None)
        for article_id, title in titles.items():
            self.assign_box.addItem(title, article_id)

        summary = self.app.assets.summary(handle)
        self.status.setText(
            f"{summary['total']} image(s) - {summary['generated']} generated, "
            f"{summary['processed']} processed, {summary['duplicates']} duplicate(s), "
            f"average quality {summary['average_quality']}"
        )

    # -------------------------------------------------------------- import
    def _import(self) -> None:
        if self.handle is None:
            return
        patterns = " ".join(f"*{s}" for s in sorted(IMAGE_SUFFIXES))
        files, _filter = QFileDialog.getOpenFileNames(
            self, "Import images", "", f"Images ({patterns});;All files (*)"
        )
        if files:
            self._import_paths([Path(f) for f in files])

    def _import_paths(self, paths: list[Path]) -> None:
        if self.handle is None:
            return
        expanded: list[Path] = []
        for path in paths:
            if path.is_dir():
                expanded.extend(
                    child for child in sorted(path.rglob("*"))
                    if child.suffix.lower() in IMAGE_SUFFIXES
                )
            else:
                expanded.append(path)

        def action() -> None:
            result = self.app.assets.import_files(self.handle, expanded)
            self.refresh()
            message = f"Imported {result.count} image(s)."
            if result.duplicates:
                message += f" {len(result.duplicates)} duplicate(s) skipped."
            if result.rejected:
                message += " Rejected: " + ", ".join(f"{f} ({r})" for f, r in result.rejected[:3])
            self.status.setText(message)

        run_guarded(self, "Import images", action)

    # ------------------------------------------------------------ details
    def _show_selected(self) -> None:
        asset_id = self.table.selected_data()
        if asset_id is None or self.handle is None:
            return
        with self.handle.uow() as uow:
            asset = uow.assets.get(asset_id)
            if asset is None:
                return
            meta = asset.meta
            analysis = meta.get("analysis", {})
            path = asset.usable_path
            index = self.assign_box.findData(asset.article_id)
            if index >= 0:
                self.assign_box.setCurrentIndex(index)
        self.canvas.load(path)
        self.detail.clear()
        for key, value in [
            ("File", Path(path).name),
            ("Pixels", f"{analysis.get('width', 0)}×{analysis.get('height', 0)}"),
            ("Orientation", analysis.get("orientation", "")),
            ("Quality", analysis.get("quality_score", 0)),
            ("Sharpness", analysis.get("sharpness", 0)),
            ("Brightness", analysis.get("brightness", 0)),
            ("Contrast", analysis.get("contrast", 0)),
            ("Colourfulness", analysis.get("colorfulness", 0)),
            ("Faces", analysis.get("face_count", 0)),
            ("Problems", ", ".join(analysis.get("problems", [])) or "none"),
            ("Provider", meta.get("generation", {}).get("provider", "-")),
            ("Prompt", (meta.get("generation", {}).get("prompt") or "")[:120]),
        ]:
            self.detail.addItem(QListWidgetItem(f"{key}: {value}"))

    # ------------------------------------------------------------- actions
    def _assign(self) -> None:
        asset_id = self.table.selected_data()
        if asset_id is None or self.handle is None:
            return
        self.app.assets.assign(self.handle, asset_id, self.assign_box.currentData())
        self.refresh()

    def _auto_assign(self) -> None:
        if self.handle is None:
            return

        def action() -> None:
            count = self.app.assets.auto_assign(self.handle)
            self.refresh()
            self.status.setText(f"Assigned {count} picture(s) to stories.")

        run_guarded(self, "Auto-assign", action)

    def _reanalyze(self) -> None:
        asset_id = self.table.selected_data()
        if self.handle is None:
            return

        def action() -> None:
            if asset_id is None:
                count = self.app.assets.analyze_all(self.handle)
                self.status.setText(f"Re-analysed {count} image(s).")
            else:
                self.app.assets.reanalyze(self.handle, asset_id)
                self.status.setText("Image re-analysed.")
            self.refresh()

        run_guarded(self, "Analyse images", action)

    def _generate(self) -> None:
        if self.handle is None:
            return
        if self.app.settings.settings.image_ai.provider == "none":
            show_error(
                self,
                "Image generation",
                "Image generation is disabled.\n\nChoose a provider on the AI Settings page; "
                "otherwise the frames are filled with a marked placeholder.",
            )
            return
        project = self.handle.project()

        def action() -> None:
            created = self.app.assets.generate_missing(
                self.handle,
                design_style=project["design_style"],
                language=project["language"],
            )
            self.refresh()
            self.status.setText(f"Generated {len(created)} image(s).")

        run_guarded(self, "Generate images", action)
