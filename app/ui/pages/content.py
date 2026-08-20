"""Content page: import, edit and order the copy."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.services.content_manager import SUPPORTED_SUFFIXES
from app.ui.pages.base import Page
from app.ui.widgets.common import (
    DataTable,
    DropArea,
    Toolbar,
    confirm,
    run_guarded,
    show_error,
)

log = logging.getLogger(__name__)


class ContentPage(Page):
    """Import sources, review the running order and edit a story."""

    title = "Content"
    subtitle = "Import TXT, DOCX, PDF, HTML, JSON or CSV - or paste text - then review the running order."
    icon = "☰"

    def build(self) -> None:
        """Create the import bar, the article table and the editor."""
        toolbar = Toolbar()
        self.import_button = toolbar.add(QPushButton("Import files…"))
        self.import_button.clicked.connect(self._import_files)
        self.paste_button = toolbar.add(QPushButton("Paste from clipboard"))
        self.paste_button.clicked.connect(self._paste)
        self.up_button = toolbar.add(QPushButton("Move up"))
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = toolbar.add(QPushButton("Move down"))
        self.down_button.clicked.connect(lambda: self._move(1))
        toolbar.stretch()
        self.headline_button = toolbar.add(QPushButton("Suggest headline"))
        self.headline_button.clicked.connect(self._suggest_headline)
        self.summary_button = toolbar.add(QPushButton("Generate summary"))
        self.summary_button.clicked.connect(self._summarize)
        self.delete_button = toolbar.add(QPushButton("Delete"))
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete)
        self.root.addWidget(toolbar)

        self.drop = DropArea(
            "Drop articles here  ({})".format(", ".join(sorted(SUPPORTED_SUFFIXES))),
            SUPPORTED_SUFFIXES,
        )
        self.drop.files_dropped.connect(self._import_paths)
        self.root.addWidget(self.drop)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = DataTable(
            ["#", "Headline", "Category", "Words", "Priority", "Page", "Area", "Image", "Approved"]
        )
        self.table.itemSelectionChanged.connect(self._load_selected)
        splitter.addWidget(self.table)
        splitter.addWidget(self._editor())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.root.addWidget(self.status)
        self.bridge.content_imported.connect(lambda _p: self.refresh())
        self._current_id: int | None = None

    def _editor(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("Story")
        form = QFormLayout(group)
        self.title_edit = QLineEdit()
        self.subtitle_edit = QLineEdit()
        self.lead_edit = QPlainTextEdit()
        self.lead_edit.setMaximumHeight(70)
        self.category_edit = QLineEdit()
        self.author_edit = QLineEdit()
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 100)
        self.page_spin = QSpinBox()
        self.page_spin.setRange(0, 200)
        self.page_spin.setSpecialValueText("auto")
        self.area_box = QComboBox()
        self.area_box.addItems(["main", "secondary", "small", "sidebar"])
        self.image_check = QCheckBox("Needs a picture")
        self.ai_image_check = QCheckBox("Generate the picture with AI")
        self.approved_check = QCheckBox("Approved - do not let the AI rewrite the display type")
        form.addRow("Headline", self.title_edit)
        form.addRow("Deck", self.subtitle_edit)
        form.addRow("Lead", self.lead_edit)
        form.addRow("Category", self.category_edit)
        form.addRow("Byline", self.author_edit)
        form.addRow("Priority", self.priority_spin)
        form.addRow("Page", self.page_spin)
        form.addRow("Area", self.area_box)
        form.addRow("", self.image_check)
        form.addRow("", self.ai_image_check)
        form.addRow("", self.approved_check)
        layout.addWidget(group)

        body_group = QGroupBox("Body (imported copy - the AI never rewrites this)")
        body_layout = QVBoxLayout(body_group)
        self.body_edit = QPlainTextEdit()
        body_layout.addWidget(self.body_edit)
        layout.addWidget(body_group, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_button = QPushButton("Save story")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        return container

    # ------------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Reload the article list."""
        handle = self.handle
        for widget in (self.import_button, self.paste_button, self.drop):
            widget.setEnabled(handle is not None)
        if handle is None:
            self.table.fill([])
            self.status.setText("Open a project to import content.")
            return
        with handle.uow() as uow:
            articles = uow.articles.for_project(handle.project_id)
            rows = [
                [
                    index + 1,
                    article.display_title[:70],
                    article.category,
                    article.word_count,
                    article.priority,
                    article.recommended_page or article.page_preference or "-",
                    article.recommended_area,
                    "yes" if article.image_required else "",
                    "yes" if article.approved else "",
                ]
                for index, article in enumerate(articles)
            ]
            ids = [article.id for article in articles]
        self.table.fill(rows, user_data=ids)
        words = sum(row[3] for row in rows)
        self.status.setText(f"{len(rows)} story/stories, {words} words.")

    # -------------------------------------------------------------- import
    def _import_files(self) -> None:
        if self.handle is None:
            return
        patterns = " ".join(f"*{s}" for s in sorted(SUPPORTED_SUFFIXES))
        files, _filter = QFileDialog.getOpenFileNames(
            self, "Import content", "", f"Supported ({patterns});;All files (*)"
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
                    child for child in sorted(path.rglob("*")) if child.suffix.lower() in SUPPORTED_SUFFIXES
                )
            else:
                expanded.append(path)

        handle = self.handle

        def done(result) -> None:
            self.refresh()
            message = f"Imported {result.count} story/stories from {len(result.files)} file(s)."
            if result.skipped:
                message += " Skipped: " + ", ".join(f"{f} ({r})" for f, r in result.skipped[:3])
            self.status.setText(message)

        self.run_background(
            "import-content",
            f"Importing {len(expanded)} file(s)",
            lambda: self.app.content.import_files(handle, expanded),
            on_success=done,
            busy_widgets=[self.import_button, self.paste_button, self.drop],
            status=self.status,
        )

    def _paste(self) -> None:
        if self.handle is None:
            return
        from PySide6.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        if not text.strip():
            show_error(self, "Paste", "The clipboard has no text.")
            return

        handle = self.handle

        def done(result) -> None:
            self.refresh()
            self.status.setText(f"Imported {result.count} story/stories from the clipboard.")

        self.run_background(
            "import-content",
            "Importing the pasted text",
            lambda: self.app.content.import_text(handle, text),
            on_success=done,
            busy_widgets=[self.import_button, self.paste_button, self.drop],
            status=self.status,
        )

    # --------------------------------------------------------------- edit
    def _load_selected(self) -> None:
        article_id = self.table.selected_data()
        self._current_id = article_id
        if article_id is None or self.handle is None:
            return
        with self.handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None:
                return
            self.title_edit.setText(article.title)
            self.subtitle_edit.setText(article.subtitle)
            self.lead_edit.setPlainText(article.lead)
            self.category_edit.setText(article.category)
            self.author_edit.setText(article.author)
            self.priority_spin.setValue(article.priority)
            self.page_spin.setValue(article.page_preference or 0)
            self.area_box.setCurrentText(article.recommended_area)
            self.image_check.setChecked(article.image_required)
            self.ai_image_check.setChecked(article.ai_image_required)
            self.approved_check.setChecked(article.approved)
            self.body_edit.setPlainText(article.body)

    def _save(self) -> None:
        if self._current_id is None or self.handle is None:
            return
        handle, article_id = self.handle, self._current_id
        before = self.app.content.snapshot_article(handle, article_id)

        def apply_edit() -> None:
            self.app.content.update_article(
                handle,
                article_id,
                title=self.title_edit.text(),
                subtitle=self.subtitle_edit.text(),
                lead=self.lead_edit.toPlainText(),
                category=self.category_edit.text() or "general",
                author=self.author_edit.text(),
                priority=self.priority_spin.value(),
                page_preference=self.page_spin.value() or None,
                recommended_area=self.area_box.currentText(),
                image_required=self.image_check.isChecked(),
                ai_image_required=self.ai_image_check.isChecked(),
                approved=self.approved_check.isChecked(),
                body=self.body_edit.toPlainText(),
            )

        def undo_edit() -> None:
            if before is not None:
                self.app.content.restore_article(handle, before)

        def action() -> None:
            self.app.undo.do("Edit story", apply_edit, undo_edit)
            self.refresh()
            self.status.setText("Story saved. Ctrl+Z undoes it.")

        run_guarded(self, "Save story", action)

    def _move(self, delta: int) -> None:
        if self.handle is None or self._current_id is None:
            return
        handle = self.handle
        before = self.app.content.snapshot_order(handle)
        if self._current_id not in before:
            return
        index = before.index(self._current_id)
        target = index + delta
        if not (0 <= target < len(before)):
            return
        after = list(before)
        after[index], after[target] = after[target], after[index]

        self.app.undo.do(
            "Reorder stories",
            lambda: self.app.content.reorder(handle, after),
            lambda: self.app.content.reorder(handle, before),
        )
        self.refresh()

    def _delete(self) -> None:
        if self._current_id is None or self.handle is None:
            return
        if not confirm(self, "Delete story", "Remove this story from the edition?"):
            return
        handle, article_id = self.handle, self._current_id
        snapshot = self.app.content.snapshot_article(handle, article_id)

        def undo_delete() -> None:
            if snapshot is not None:
                self.app.content.restore_article(handle, snapshot)

        self.app.undo.do(
            "Delete story",
            lambda: self.app.content.delete_article(handle, article_id),
            undo_delete,
        )
        self._current_id = None
        self.refresh()
        self.status.setText("Story deleted. Ctrl+Z brings it back.")

    # ----------------------------------------------------------------- ai
    def _suggest_headline(self) -> None:
        if self._current_id is None or self.handle is None:
            return
        from app.agents.editorial_agent import EditorialAgent

        handle, article_id = self.handle, self._current_id

        def done(suggestion) -> None:
            self.title_edit.setText(suggestion.headline)
            if suggestion.subtitle:
                self.subtitle_edit.setText(suggestion.subtitle)
            alternatives = "  |  ".join(suggestion.alternatives)
            self.status.setText(
                f"Suggested headline applied to the editor - alternatives: {alternatives}"
                if alternatives
                else "Suggested headline applied to the editor. Save to keep it."
            )

        self.run_background(
            "headline",
            "Suggesting a headline",
            lambda: EditorialAgent(self.app.ai).suggest_headline(handle, article_id),
            on_success=done,
            busy_widgets=[self.headline_button, self.summary_button],
            status=self.status,
        )

    def _summarize(self) -> None:
        if self._current_id is None or self.handle is None:
            return
        from app.agents.editorial_agent import EditorialAgent

        handle, article_id = self.handle, self._current_id

        def done(data) -> None:
            self.lead_edit.setPlainText(data.get("lead", ""))
            self.status.setText("Lead and summary generated.")

        self.run_background(
            "summary",
            "Generating the summary",
            lambda: EditorialAgent(self.app.ai).summarize(handle, article_id),
            on_success=done,
            busy_widgets=[self.headline_button, self.summary_button],
            status=self.status,
        )
