"""Studio page: write what you want, the crew makes it.

One request, whatever material there is to go with it, and three specialists
working in Photoshop, InDesign and Premiere at the same time. When the art
director needs to know something it cannot decide, this page shows the
question as a card and waits; everything else it decides itself.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.agents.studio import Brief, Question, StudioRun
from app.core.jobs import CancelToken
from app.ui.pages.base import Page
from app.ui.pages.preview import _open_path
from app.ui.widgets.common import Card, Toolbar

log = logging.getLogger(__name__)

REFERENCE_FILTER = "Images and clips (*.png *.jpg *.jpeg *.tif *.tiff *.webp *.mp4 *.mov *.mkv)"
FOOTAGE_FILTER = "Footage and audio (*.mp4 *.mov *.mkv *.avi *.m4v *.mp3 *.wav *.aac *.m4a)"

HOSTS = [
    ("photoshop", "Photoshop", "Posters, covers, composites, the boxes a page is built from"),
    ("indesign", "InDesign", "Documents, spreads, columns, the type on the page"),
    ("premiere", "Premiere Pro", "Cuts, transitions, titles and graphics over footage"),
]


class StudioPage(Page):
    """Where a request becomes finished work."""

    title = "Studio"
    subtitle = (
        "Say what you want and give the crew your material. The art director decides which "
        "applications to use - or asks - and the specialists work in them at the same time."
    )
    icon = "◈"

    def build(self) -> None:
        """Create the brief on the left and the outcome on the right."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._brief_side())
        splitter.addWidget(self._outcome_side())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        self.root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.status.setWordWrap(True)
        self.root.addWidget(self.status)

        self.token: CancelToken | None = None
        self.answers: dict[str, str] = {}
        self.last_run: StudioRun | None = None
        self._question_widgets: list[tuple[Question, QButtonGroup]] = []

    # ---------------------------------------------------------- the brief
    def _brief_side(self) -> QWidget:
        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 8, 0)

        brief_card = Card("The brief")
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("A working name, used for the file names")
        form.addRow("Name", self.title_edit)
        self.request_edit = QPlainTextEdit()
        self.request_edit.setPlaceholderText(
            "What do you want made? For example: a front page for tomorrow's budget report, "
            "and a reel to go with it."
        )
        self.request_edit.setMinimumHeight(80)
        form.addRow("Request", self.request_edit)
        self.format_edit = QLineEdit()
        self.format_edit.setPlaceholderText("A3, 381x476 mm, instagram reel, 1080x1350 px…")
        form.addRow("Size", self.format_edit)
        self.language_edit = QLineEdit("fa")
        self.language_edit.setMaximumWidth(80)
        form.addRow("Language", self.language_edit)
        holder = QWidget()
        holder.setLayout(form)
        brief_card.add(holder)
        layout.addWidget(brief_card)

        content_card = Card("The material")
        self.content_edit = QPlainTextEdit()
        self.content_edit.setPlaceholderText(
            "The copy itself. A kicker in brackets on its own line, the headline next, then the "
            "body. A paragraph beginning 'کادر:' or 'Sidebar:' becomes a box; one beginning "
            "'عکس:' or 'Caption:' becomes a caption."
        )
        content_card.add(self.content_edit)
        layout.addWidget(content_card, 1)

        hosts_group = QGroupBox("Applications")
        hosts_layout = QVBoxLayout(hosts_group)
        hint = QLabel("Leave all three unticked to let the art director decide.")
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
        hosts_layout.addWidget(hint)
        self.host_checks: dict[str, QCheckBox] = {}
        for host, label, trade in HOSTS:
            check = QCheckBox(f"{label} - {trade}")
            self.host_checks[host] = check
            hosts_layout.addWidget(check)
        layout.addWidget(hosts_group)

        files_card = Card("Attachments")
        self.reference_list = QListWidget()
        self.reference_list.setMaximumHeight(84)
        files_card.add(QLabel("References - an image or clip whose style the work should inherit"))
        files_card.add(self.reference_list)
        reference_bar = Toolbar()
        reference_bar.add(QPushButton("Add references…")).clicked.connect(self._add_references)
        reference_bar.add(QPushButton("Clear")).clicked.connect(self.reference_list.clear)
        reference_bar.stretch()
        files_card.add(reference_bar)
        self.footage_list = QListWidget()
        self.footage_list.setMaximumHeight(84)
        files_card.add(QLabel("Footage and audio to cut"))
        files_card.add(self.footage_list)
        footage_bar = Toolbar()
        footage_bar.add(QPushButton("Add footage…")).clicked.connect(self._add_footage)
        footage_bar.add(QPushButton("Clear")).clicked.connect(self.footage_list.clear)
        footage_bar.stretch()
        files_card.add(footage_bar)
        layout.addWidget(files_card)

        harvest_card = Card("Work like an existing publication")
        note = QLabel(
            "Point at a PDF of a newspaper or magazine and the studio measures it - its "
            "sheet, margins, column grid, colours, type scale and the boxes its pages are "
            "built from - then lays your pages out in that publication's own idiom, with "
            "its boxes redrawn rather than reinvented."
        )
        note.setObjectName("Subtitle")
        note.setWordWrap(True)
        harvest_card.add(note)
        harvest_bar = Toolbar()
        self.harvest_button = harvest_bar.add(QPushButton("Measure a publication…"))
        self.harvest_button.clicked.connect(self._harvest)
        self.forget_button = harvest_bar.add(QPushButton("Forget it"))
        self.forget_button.setEnabled(False)
        self.forget_button.clicked.connect(self._forget)
        harvest_bar.stretch()
        harvest_card.add(harvest_bar)
        self.harvest_label = QLabel("Nothing has been measured.")
        self.harvest_label.setObjectName("Subtitle")
        self.harvest_label.setWordWrap(True)
        harvest_card.add(self.harvest_label)
        layout.addWidget(harvest_card)

        toolbar = Toolbar()
        self.run_button = toolbar.add(QPushButton("Make it"))
        self.run_button.setObjectName("Primary")
        self.run_button.clicked.connect(self._run)
        self.cancel_button = toolbar.add(QPushButton("Stop"))
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        toolbar.stretch()
        self.folder_button = toolbar.add(QPushButton("Open the studio folder"))
        self.folder_button.clicked.connect(self._open_folder)
        layout.addWidget(toolbar)
        return side

    # -------------------------------------------------------- the outcome
    def _outcome_side(self) -> QWidget:
        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(8, 0, 0, 0)

        self.questions_card = Card("The art director would like to know")
        self.questions_holder = QWidget()
        self.questions_layout = QVBoxLayout(self.questions_holder)
        self.questions_layout.setContentsMargins(0, 0, 0, 0)
        self.questions_card.add(self.questions_holder)
        self.answer_button = QPushButton("Use these answers and carry on")
        self.answer_button.setObjectName("Primary")
        self.answer_button.clicked.connect(self._answer)
        self.questions_card.add(self.answer_button)
        self.questions_card.setVisible(False)
        layout.addWidget(self.questions_card)

        concept_card = Card("The concept")
        self.concept_label = QLabel("Nothing has been made yet.")
        self.concept_label.setWordWrap(True)
        concept_card.add(self.concept_label)
        layout.addWidget(concept_card)

        crew_card = Card("The crew")
        self.crew_list = QListWidget()
        crew_card.add(self.crew_list)
        layout.addWidget(crew_card, 1)

        files_card = Card("What was made")
        self.files_list = QListWidget()
        self.files_list.itemDoubleClicked.connect(self._open_item)
        files_card.add(self.files_list)
        layout.addWidget(files_card, 2)
        return side

    # ------------------------------------------------------------ actions
    def _harvest(self) -> None:
        """Measure a publication the operator points at."""
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a publication", "", "Publications (*.pdf)"
        )
        if not chosen:
            return
        service = self.app.studio
        name = Path(chosen).stem
        self.run_background(
            "harvest",
            f"Measuring {Path(chosen).name}",
            lambda: service.harvest(chosen, name=name, pages=8),
            on_success=self._show_system,
            busy_widgets=[self.harvest_button, self.run_button],
            status=self.status,
        )

    def _show_system(self, system: object) -> None:
        """Say what was read, and how sure of it the measurement is."""
        describes = getattr(system, "describe", lambda: "")()
        confidence = getattr(system, "confidence", {}) or {}
        weakest = sorted(confidence.items(), key=lambda item: item[1])[:2]
        lines = [f"<b>{getattr(system, 'name', '') or 'Publication'}</b>", describes]
        if weakest:
            lines.append(
                "Least certain of: "
                + ", ".join(f"{name} ({value * 100:.0f}%)" for name, value in weakest)
            )
        for note in getattr(system, "notes", [])[:3]:
            lines.append(f"· {note}")
        self.harvest_label.setText("<br>".join(lines))
        self.forget_button.setEnabled(True)
        self.status.setText(
            "Measured. New pages will be laid out in this publication's idiom, with its "
            "own boxes."
        )

    def _forget(self) -> None:
        """Stop working in the measured publication's idiom."""
        self.app.studio.systems.clear()
        self.harvest_label.setText("Nothing has been measured.")
        self.forget_button.setEnabled(False)

    def _add_references(self) -> None:
        self._add_files(self.reference_list, "Choose references", REFERENCE_FILTER)

    def _add_footage(self) -> None:
        self._add_files(self.footage_list, "Choose footage", FOOTAGE_FILTER)

    def _add_files(self, target: QListWidget, caption: str, file_filter: str) -> None:
        chosen, _ = QFileDialog.getOpenFileNames(self, caption, "", file_filter)
        existing = {target.item(row).text() for row in range(target.count())}
        for path in chosen:
            if path not in existing:
                target.addItem(path)

    def _brief(self) -> Brief:
        """Read the form into a brief."""
        return Brief(
            request=self.request_edit.toPlainText().strip(),
            content=self.content_edit.toPlainText().strip(),
            references=[self.reference_list.item(i).text() for i in range(self.reference_list.count())],
            footage=[self.footage_list.item(i).text() for i in range(self.footage_list.count())],
            hosts=[host for host, check in self.host_checks.items() if check.isChecked()],
            format=self.format_edit.text().strip(),
            language=self.language_edit.text().strip() or "fa",
            title=self.title_edit.text().strip(),
            answers=dict(self.answers),
        )

    def _run(self) -> None:
        brief = self._brief()
        if not brief.request and not brief.content:
            self.status.setText("Say what you want made, or paste the copy to make it from.")
            return
        self.token = CancelToken()
        self.cancel_button.setEnabled(True)
        self.crew_list.clear()
        self.files_list.clear()
        token = self.token
        service = self.app.studio
        self.run_background(
            "studio",
            "The studio is working",
            lambda: service.run(brief, token=token),
            on_success=self._show,
            busy_widgets=[self.run_button],
            status=self.status,
        )

    def _cancel(self) -> None:
        if self.token is not None:
            self.token.cancel()
            self.status.setText("Stopping…")
        self.cancel_button.setEnabled(False)

    def _answer(self) -> None:
        """Record what was chosen on the cards and run again."""
        for question, group in self._question_widgets:
            button = group.checkedButton()
            if button is not None:
                self.answers[question.id] = button.text()
        chosen = self.answers.get("format")
        if chosen and not self.format_edit.text().strip():
            self.format_edit.setText(_size_from(chosen))
        self._run()

    # ------------------------------------------------------------ results
    def _show(self, run: StudioRun) -> None:
        """Put the outcome on the page."""
        self.last_run = run
        self.cancel_button.setEnabled(False)
        self._show_questions(run.questions)

        concept = run.concept
        parts = [f"<b>{concept.name or 'Untitled'}</b>"]
        if concept.rationale:
            parts.append(concept.rationale)
        parts.append(
            f"Planned by the {'model' if concept.source == 'model' else 'router'}; "
            f"{len(concept.assignments)} assignment(s)."
        )
        self.concept_label.setText("<br>".join(parts))

        self.crew_list.clear()
        for result in run.results:
            assignment = result.assignment
            if result.ok:
                mark, detail = "✓", f"{result.run.tool_calls} step(s)" if result.run else ""
            elif result.skipped:
                mark, detail = "–", result.skipped
            else:
                mark, detail = "✗", result.error or (result.run.stop_reason if result.run else "")
            item = QListWidgetItem(f"{mark}  {assignment.host}  ·  {assignment.goal}\n     {detail}")
            self.crew_list.addItem(item)
        for note in run.notes:
            self.crew_list.addItem(QListWidgetItem(f"     {note['author']}: {note['text']}"))

        self.files_list.clear()
        for path in run.files:
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.files_list.addItem(item)

        if run.stop_reason == "waiting for an answer":
            self.status.setText("The art director needs an answer before it can start.")
        elif run.finished:
            self.status.setText(
                f"Finished in {run.duration:.0f}s. {len(run.files)} file(s) - double-click to open one."
            )
        else:
            self.status.setText(f"Stopped: {run.stop_reason}")

    def _show_questions(self, questions: list[Question]) -> None:
        """Draw the interactive cards, or hide them when there are none."""
        while self.questions_layout.count():
            item = self.questions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._question_widgets = []
        if not questions:
            self.questions_card.setVisible(False)
            return

        for question in questions:
            heading = QLabel(f"<b>{question.question}</b>")
            heading.setWordWrap(True)
            self.questions_layout.addWidget(heading)
            if question.why:
                why = QLabel(question.why)
                why.setObjectName("Subtitle")
                why.setWordWrap(True)
                self.questions_layout.addWidget(why)
            group = QButtonGroup(self.questions_holder)
            for option in question.options:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                button = QRadioButton(option.label)
                if option.recommended:
                    button.setChecked(True)
                    button.setToolTip("Recommended")
                group.addButton(button)
                row_layout.addWidget(button)
                if option.detail:
                    detail = QLabel(option.detail)
                    detail.setObjectName("Subtitle")
                    detail.setWordWrap(True)
                    row_layout.addWidget(detail, 1)
                self.questions_layout.addWidget(row)
            self._question_widgets.append((question, group))
        self.questions_card.setVisible(True)

    def _open_item(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            _open_path(Path(path))

    def _open_folder(self) -> None:
        _open_path(self.app.studio.context().workspace)


def _size_from(label: str) -> str:
    """The measurement out of an option's label.

    The cards read "A3, 297x420 mm", which is for the operator; the size field
    wants the part the format registry can parse.
    """
    if "," in label:
        return label.split(",", 1)[1].strip()
    return label.strip()
