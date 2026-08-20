"""Export page: PDF presets and the produced deliverables."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
)

from app.models.schemas import LayoutPlan
from app.ui.pages.base import Page
from app.ui.pages.preview import _open_path
from app.ui.widgets.common import Card, Toolbar, show_error

log = logging.getLogger(__name__)


class ExportPage(Page):
    """Choose the presets and produce the final files."""

    title = "Export"
    subtitle = "Print, high-quality, digital and web PDFs, plus the InDesign document, IDML and previews."
    icon = "⬇"

    def build(self) -> None:
        """Create the preset selection and the output list."""
        presets_group = QGroupBox("Presets")
        form = QFormLayout(presets_group)
        self.preset_checks: dict[str, QCheckBox] = {}
        for preset_id, caption in [
            ("print", "Print - CMYK with bleed and marks"),
            ("high_quality", "High quality archive"),
            ("digital", "Digital edition - RGB"),
            ("web", "Web / e-mail - small"),
        ]:
            check = QCheckBox(caption)
            check.setChecked(preset_id == "print")
            self.preset_checks[preset_id] = check
            form.addRow("", check)
        self.indd_check = QCheckBox("Save the InDesign document (.indd)")
        self.idml_check = QCheckBox("Export IDML")
        self.previews_check = QCheckBox("Export page previews")
        self.archive_check = QCheckBox("Create a project archive (.zip)")
        self.indd_check.setChecked(True)
        self.idml_check.setChecked(True)
        self.previews_check.setChecked(True)
        for check in (self.indd_check, self.idml_check, self.previews_check, self.archive_check):
            form.addRow("", check)
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(36, 600)
        self.dpi_spin.setValue(110)
        form.addRow("Preview resolution (dpi)", self.dpi_spin)
        self.root.addWidget(presets_group)

        toolbar = Toolbar()
        self.export_button = toolbar.add(QPushButton("Export now"))
        self.export_button.setObjectName("Primary")
        self.export_button.clicked.connect(self._export)
        self.package_button = toolbar.add(QPushButton("Package for print (InDesign)"))
        self.package_button.clicked.connect(self._package)
        toolbar.stretch()
        self.open_button = toolbar.add(QPushButton("Open the output folder"))
        self.open_button.clicked.connect(self._open_folder)
        self.root.addWidget(toolbar)

        outputs = Card("Deliverables")
        self.output_list = QListWidget()
        self.output_list.itemDoubleClicked.connect(self._open_item)
        outputs.add(self.output_list)
        self.root.addWidget(outputs, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.status.setWordWrap(True)
        self.root.addWidget(self.status)
        self.bridge.export_ready.connect(lambda _p: self.refresh())

    def refresh(self) -> None:
        """List the files already in the output folder."""
        handle = self.handle
        self.export_button.setEnabled(handle is not None)
        self.output_list.clear()
        if handle is None:
            self.status.setText("Open a project to export it.")
            return
        self.dpi_spin.setValue(self.app.settings.settings.export.preview_dpi)
        files = sorted(
            (p for p in handle.output_dir.rglob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in files[:200]:
            size = path.stat().st_size
            item = QListWidgetItem(f"{path.relative_to(handle.output_dir)}   ({size / 1024:.0f} KB)")
            item.setData(1000, str(path))
            self.output_list.addItem(item)
        self.status.setText(
            f"{len(files)} file(s) in {handle.output_dir}" if files else "Nothing exported yet."
        )

    # -------------------------------------------------------------- actions
    def _export(self) -> None:
        handle = self.handle
        if handle is None:
            return
        if not handle.layout_plan_path.exists():
            show_error(self, "Export", "There is no layout plan yet. Generate the edition first.")
            return
        presets = [pid for pid, check in self.preset_checks.items() if check.isChecked()]
        if not presets:
            show_error(self, "Export", "Select at least one PDF preset.")
            return

        def work():
            plan = LayoutPlan.load(handle.layout_plan_path)
            template = self.app.templates.get_or_default(plan.template_id)
            self.app.exporter.indesign = (
                self.app.adobe.indesign if self.app.adobe.indesign_app.installed else None
            )
            return self.app.exporter.export(
                handle,
                plan,
                template,
                presets=presets,
                export_indd=indd,
                export_idml=idml,
                export_previews=previews,
                preview_dpi=dpi,
                archive=archive,
            )

        def done(result) -> None:
            self.refresh()
            message = f"Exported with the {result.engine} engine: {', '.join(result.pdfs)}"
            if result.warnings:
                message += "\n" + "\n".join(f"• {w}" for w in result.warnings[:4])
            self.status.setText(message)

        indd = self.indd_check.isChecked()
        idml = self.idml_check.isChecked()
        previews = self.previews_check.isChecked()
        dpi = self.dpi_spin.value()
        archive = self.archive_check.isChecked()
        self.run_background(
            "export",
            "Exporting",
            work,
            on_success=done,
            busy_widgets=[self.export_button, self.package_button],
            status=self.status,
        )

    def _package(self) -> None:
        handle = self.handle
        if handle is None:
            return
        if not self.app.adobe.indesign_app.installed:
            show_error(self, "Package", "Packaging collects links and fonts and requires InDesign.")
            return

        def work():
            self.app.exporter.indesign = self.app.adobe.indesign
            return self.app.exporter.package(handle)

        def done(folder) -> None:
            self.status.setText(f"Packaged into {folder}")
            self.refresh()

        self.run_background(
            "package",
            "Packaging for print",
            work,
            on_success=done,
            busy_widgets=[self.export_button, self.package_button],
            status=self.status,
        )

    def _open_folder(self) -> None:
        if self.handle is not None:
            _open_path(self.handle.output_dir)

    def _open_item(self, item: QListWidgetItem) -> None:
        path = item.data(1000)
        if path:
            _open_path(Path(path))
