"""Projects and the New Project wizard."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from app.models.schemas import ProjectSpec
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, DataTable, Toolbar, confirm, run_guarded, show_error
from app.utils.units import PAGE_SIZES_MM

log = logging.getLogger(__name__)


class ProjectsPage(Page):
    """List, open, duplicate, archive and delete projects."""

    title = "Projects"
    subtitle = "Every edition is a self-contained folder with its own database, assets and output."
    icon = "▤"

    def build(self) -> None:
        """Create the project table and its toolbar."""
        toolbar = Toolbar()
        self.open_button = toolbar.add(QPushButton("Open"))
        self.open_button.clicked.connect(self._open_selected)
        self.folder_button = toolbar.add(QPushButton("Open folder…"))
        self.folder_button.clicked.connect(self._open_folder)
        self.duplicate_button = toolbar.add(QPushButton("Duplicate"))
        self.duplicate_button.clicked.connect(self._duplicate)
        self.archive_button = toolbar.add(QPushButton("Archive"))
        self.archive_button.clicked.connect(self._archive)
        toolbar.stretch()
        self.delete_button = toolbar.add(QPushButton("Delete"))
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete)
        self.root.addWidget(toolbar)

        self.table = DataTable(
            ["Name", "Publication", "Edition", "Pages", "Template", "Status", "Folder"]
        )
        self.table.doubleClicked.connect(self._open_selected)
        self.root.addWidget(self.table, 1)

        self.info = QLabel("")
        self.info.setObjectName("Subtitle")
        self.root.addWidget(self.info)

    def refresh(self) -> None:
        """Reload the project registry."""
        rows = self.app.projects.list_projects()
        self.table.fill(
            [
                [
                    row.get("name", ""),
                    row.get("publication_name", ""),
                    str(row.get("edition_date") or ""),
                    row.get("page_count", 0),
                    row.get("template_id", ""),
                    row.get("status", ""),
                    row.get("directory", ""),
                ]
                for row in rows
            ],
            user_data=[row.get("slug") or row.get("directory") for row in rows],
        )
        current = self.app.current.slug if self.app.current else "none"
        self.info.setText(f"{len(rows)} project(s). Current: {current}")

    # --------------------------------------------------------------- actions
    def _open_selected(self) -> None:
        slug = self.table.selected_data()
        if not slug:
            return
        window = self.window()
        opener = getattr(window, "open_project", None)
        if callable(opener):
            run_guarded(self, "Open project", lambda: opener(slug))

    def _open_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select a project folder")
        if not directory:
            return
        window = self.window()
        opener = getattr(window, "open_project", None)
        if callable(opener):
            run_guarded(self, "Open project", lambda: opener(Path(directory)))

    def _duplicate(self) -> None:
        slug = self.table.selected_data()
        if not slug:
            return

        def action() -> None:
            handle = self.app.projects.open(slug)
            copy = self.app.projects.duplicate(handle, f"{handle.project()['name']} (copy)")
            log.info("Duplicated %s -> %s", slug, copy.slug)
            self.refresh()

        run_guarded(self, "Duplicate project", action)

    def _archive(self) -> None:
        slug = self.table.selected_data()
        if not slug:
            return

        def action() -> None:
            handle = self.app.projects.open(slug)
            archive = self.app.projects.archive(handle)
            self.info.setText(f"Archived to {archive}")

        run_guarded(self, "Archive project", action)

    def _delete(self) -> None:
        slug = self.table.selected_data()
        if not slug:
            return
        if not confirm(
            self,
            "Delete project",
            f"Delete '{slug}' and every file in its folder?\nThis cannot be undone.",
        ):
            return

        def action() -> None:
            self.app.projects.delete(str(slug), remove_files=True)
            if self.app.current and self.app.current.slug == slug:
                self.app.current = None
            self.refresh()

        run_guarded(self, "Delete project", action)


class NewProjectPage(Page):
    """The New Project wizard (specification §20)."""

    title = "New Project"
    subtitle = "Create an edition: name it, choose the format, the template and the AI provider."
    icon = "＋"

    def build(self) -> None:
        """Create the form."""
        card = Card("Edition")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Morning Post - 2026-08-20")
        self.publication_edit = QLineEdit()
        self.publication_edit.setPlaceholderText("Morning Post")
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.language_box = QComboBox()
        self.language_box.addItems(["fa", "en", "ar", "tr"])
        self.product_box = QComboBox()
        self.product_box.addItems(
            ["newspaper", "magazine", "brochure", "catalog", "flyer", "poster", "digital"]
        )
        self.size_box = QComboBox()
        self.size_box.addItems(sorted(PAGE_SIZES_MM))
        self.size_box.setCurrentText("Broadsheet")
        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(1, 200)
        self.pages_spin.setValue(8)
        self.template_box = QComboBox()
        self.style_box = QComboBox()
        self.style_box.addItems(["classic", "modern", "compact", "feature", "bold"])
        self.provider_box = QComboBox()
        self.provider_box.addItems(["heuristic", "openai", "anthropic", "gemini", "local"])

        form.addRow("Project name", self.name_edit)
        form.addRow("Publication", self.publication_edit)
        form.addRow("Edition date", self.date_edit)
        form.addRow("Language", self.language_box)
        form.addRow("Product", self.product_box)
        form.addRow("Page size", self.size_box)
        form.addRow("Pages", self.pages_spin)
        form.addRow("Template", self.template_box)
        form.addRow("Design style", self.style_box)
        form.addRow("AI provider", self.provider_box)
        card.body().addLayout(form)
        self.root.addWidget(card)

        toolbar = Toolbar()
        toolbar.stretch()
        self.create_button = toolbar.add(QPushButton("Create project"))
        self.create_button.setObjectName("Primary")
        self.create_button.clicked.connect(self._create)
        self.root.addWidget(toolbar)
        self.root.addStretch(1)

        self.product_box.currentTextChanged.connect(lambda _t: self._reload_templates())
        self.publication_edit.textChanged.connect(self._suggest_name)

    def refresh(self) -> None:
        """Reload the template list and pre-fill the provider."""
        self._reload_templates()
        self.provider_box.setCurrentText(self.app.settings.settings.ai.provider)
        self.language_box.setCurrentText(self.app.settings.settings.ui.language)

    def _reload_templates(self) -> None:
        product = self.product_box.currentText()
        summaries = self.app.templates.list_summaries(product) or self.app.templates.list_summaries()
        self.template_box.clear()
        for summary in summaries:
            self.template_box.addItem(f"{summary['name']}  ({summary['id']})", summary["id"])
        default = self.app.settings.settings.default_template_id
        index = self.template_box.findData(default)
        if index >= 0:
            self.template_box.setCurrentIndex(index)

    def _suggest_name(self, publication: str) -> None:
        if not self.name_edit.text().strip() or self.name_edit.property("auto"):
            from app.services.project_manager import default_edition_name

            self.name_edit.setText(default_edition_name(publication, date.today()))
            self.name_edit.setProperty("auto", True)

    def _create(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            show_error(self, "New project", "The project needs a name.")
            return
        template_id = self.template_box.currentData() or self.app.settings.settings.default_template_id
        width, height = PAGE_SIZES_MM.get(self.size_box.currentText(), (297.0, 420.0))
        spec = ProjectSpec(
            name=name,
            publication_name=self.publication_edit.text().strip() or name,
            edition_date=self.date_edit.date().toPython(),
            language=self.language_box.currentText(),  # type: ignore[arg-type]
            product_type=self.product_box.currentText(),  # type: ignore[arg-type]
            page_size=self.size_box.currentText(),
            page_width_mm=width,
            page_height_mm=height,
            page_count=self.pages_spin.value(),
            template_id=str(template_id),
            design_style=self.style_box.currentText(),
            ai_provider=self.provider_box.currentText(),
        )

        def action() -> None:
            handle = self.app.create_project(spec)
            window = self.window()
            notify = getattr(window, "project_opened", None)
            if callable(notify):
                notify(handle)

        run_guarded(self, "Create project", action)
