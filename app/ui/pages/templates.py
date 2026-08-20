"""Templates page: browse, validate, duplicate, import and export templates."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
)

from app.ui.pages.base import Page
from app.ui.widgets.common import DataTable, Toolbar, confirm, run_guarded

log = logging.getLogger(__name__)


class TemplatesPage(Page):
    """The template catalogue."""

    title = "Templates"
    subtitle = "A template defines the trim size, grid, margins, bleed, colours, styles and master pages."
    icon = "▦"

    def build(self) -> None:
        """Create the catalogue table and the detail panel."""
        toolbar = Toolbar()
        self.use_button = toolbar.add(QPushButton("Use for this project"))
        self.use_button.clicked.connect(self._use)
        self.default_button = toolbar.add(QPushButton("Set as default"))
        self.default_button.clicked.connect(self._set_default)
        self.duplicate_button = toolbar.add(QPushButton("Duplicate"))
        self.duplicate_button.clicked.connect(self._duplicate)
        toolbar.stretch()
        self.import_button = toolbar.add(QPushButton("Import…"))
        self.import_button.clicked.connect(self._import)
        self.export_button = toolbar.add(QPushButton("Export…"))
        self.export_button.clicked.connect(self._export)
        self.delete_button = toolbar.add(QPushButton("Delete"))
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete)
        self.root.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = DataTable(["Name", "Id", "Product", "Language", "Page", "Columns", "Styles", "Built-in"])
        self.table.itemSelectionChanged.connect(self._show_selected)
        splitter.addWidget(self.table)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.root.addWidget(self.status)

    def refresh(self) -> None:
        """Reload the catalogue."""
        summaries = self.app.templates.list_summaries()
        self.table.fill(
            [
                [
                    s["name"],
                    s["id"],
                    s["product_type"],
                    s["language"],
                    s["page"],
                    s["columns"],
                    s["styles"],
                    "yes" if s["builtin"] else "",
                ]
                for s in summaries
            ],
            user_data=[s["id"] for s in summaries],
        )
        current = self.handle.project()["template_id"] if self.handle else "-"
        self.status.setText(
            f"{len(summaries)} template(s). Project template: {current}. "
            f"Default: {self.app.settings.settings.default_template_id}"
        )

    def _show_selected(self) -> None:
        template_id = self.table.selected_data()
        if not template_id:
            return

        def action() -> None:
            spec = self.app.templates.get(template_id)
            warnings = self.app.templates.validate(template_id)
            lines = [
                f"{spec.name}  ({spec.id})",
                spec.description,
                "",
                f"Page      : {spec.page_width_mm} × {spec.page_height_mm} mm, bleed {spec.bleed_mm} mm",
                f"Margins   : top {spec.margins.top}, bottom {spec.margins.bottom}, "
                f"inside {spec.margins.inside}, outside {spec.margins.outside} mm",
                f"Grid      : {spec.grid.columns} columns, gutter {spec.grid.gutter_mm} mm, "
                f"baseline {spec.grid.baseline_mm} mm",
                f"Column    : {spec.column_width_mm():.2f} mm",
                f"Direction : {spec.direction}   Language: {spec.language}",
                "",
                "Paragraph styles:",
            ]
            for style in spec.paragraph_styles:
                lines.append(
                    f"  {style.id:<14} {style.font_family} {style.font_style} "
                    f"{style.size_pt}/{style.leading_pt} pt  {style.alignment}"
                )
            lines.append("")
            lines.append("Master pages: " + ", ".join(m.name for m in spec.master_pages))
            lines.append("PDF presets : " + ", ".join(p.id for p in spec.pdf_presets))
            if warnings:
                lines.append("")
                lines.append("Warnings:")
                lines.extend(f"  ! {w}" for w in warnings)
            self.detail.setPlainText("\n".join(lines))

        run_guarded(self, "Show template", action)

    # -------------------------------------------------------------- actions
    def _use(self) -> None:
        template_id = self.table.selected_data()
        if not template_id or self.handle is None:
            return
        self.app.projects.update(self.handle, template_id=str(template_id))
        self.refresh()

    def _set_default(self) -> None:
        template_id = self.table.selected_data()
        if not template_id:
            return
        self.app.settings.set_path("default_template_id", str(template_id))
        self.refresh()

    def _duplicate(self) -> None:
        template_id = self.table.selected_data()
        if not template_id:
            return
        new_id, ok = QInputDialog.getText(self, "Duplicate template", "New template id:")
        if not ok or not new_id.strip():
            return

        def action() -> None:
            spec = self.app.templates.duplicate(str(template_id), new_id.strip())
            self.refresh()
            self.status.setText(f"Created '{spec.id}'.")

        run_guarded(self, "Duplicate template", action)

    def _import(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, "Import template", "", "Templates (*.template.json);;All files (*)"
        )
        if not path:
            return

        def action() -> None:
            spec = self.app.templates.import_file(Path(path))
            self.refresh()
            self.status.setText(f"Imported '{spec.id}'.")

        run_guarded(self, "Import template", action)

    def _export(self) -> None:
        template_id = self.table.selected_data()
        if not template_id:
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Export template", f"{template_id}.template.json", "Templates (*.template.json)"
        )
        if not path:
            return

        def action() -> None:
            target = self.app.templates.export_file(str(template_id), Path(path))
            self.status.setText(f"Exported to {target}")

        run_guarded(self, "Export template", action)

    def _delete(self) -> None:
        template_id = self.table.selected_data()
        if not template_id:
            return
        if not confirm(self, "Delete template", f"Delete the template '{template_id}'?"):
            return

        def action() -> None:
            self.app.templates.delete(str(template_id))
            self.refresh()

        run_guarded(self, "Delete template", action)
