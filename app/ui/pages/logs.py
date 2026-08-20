"""Logs page: the live log stream and the job queue."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
)

from app.core.logging_setup import dump_records, get_buffer
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, DataTable, Toolbar, run_guarded

log = logging.getLogger(__name__)

LEVEL_COLORS = {
    "DEBUG": "#8a94a6",
    "INFO": "#c8cdd6",
    "WARNING": "#e0a33e",
    "ERROR": "#e0554a",
    "CRITICAL": "#ff6b5e",
}


class LogsPage(Page):
    """Live log lines and the state of every queued job."""

    title = "Logs"
    subtitle = "Every operation is logged; long work runs in the job queue so the interface never freezes."
    icon = "≡"

    def build(self) -> None:
        """Create the log view, the filters and the job table."""
        toolbar = Toolbar()
        self.level_box = QComboBox()
        self.level_box.addItems(["ALL", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.level_box.setCurrentText("INFO")
        self.level_box.currentTextChanged.connect(lambda _t: self._reload())
        toolbar.add(QLabel("Level:"))
        toolbar.add(self.level_box)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter…")
        self.filter_edit.textChanged.connect(lambda _t: self._reload())
        toolbar.add(self.filter_edit)
        self.follow_check = QCheckBox("Follow")
        self.follow_check.setChecked(True)
        toolbar.add(self.follow_check)
        toolbar.stretch()
        self.clear_button = toolbar.add(QPushButton("Clear"))
        self.clear_button.clicked.connect(self._clear)
        self.save_button = toolbar.add(QPushButton("Save to file…"))
        self.save_button.clicked.connect(self._save)
        self.open_button = toolbar.add(QPushButton("Open the log folder"))
        self.open_button.clicked.connect(self._open_folder)
        self.root.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(6000)
        self.view.setStyleSheet("font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 12px;")
        splitter.addWidget(self.view)

        jobs_card = Card("Jobs")
        self.jobs = DataTable(["Job", "Lane", "State", "Progress", "Message", "Duration"])
        jobs_card.add(self.jobs)
        splitter.addWidget(jobs_card)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.root.addWidget(self.status)

        self.bridge.log_line.connect(self._append)
        self.bridge.job_changed.connect(lambda _p: self._reload_jobs())

    def refresh(self) -> None:
        """Reload the buffered log lines and the job table."""
        self._reload()
        self._reload_jobs()

    # ----------------------------------------------------------------- log
    def _reload(self) -> None:
        level = self.level_box.currentText()
        needle = self.filter_edit.text().strip()
        records = get_buffer().records(None if level == "ALL" else level, needle or None)
        self.view.clear()
        for record in records[-3000:]:
            self._write(record)
        self.status.setText(f"{len(records)} line(s) buffered.")

    def _append(self, record: dict) -> None:
        level = self.level_box.currentText()
        if level != "ALL" and record.get("level") != level:
            if not (level == "INFO" and record.get("level") in ("WARNING", "ERROR", "CRITICAL")):
                return
        needle = self.filter_edit.text().strip().lower()
        if (
            needle
            and needle not in record.get("message", "").lower()
            and needle not in record.get("logger", "").lower()
        ):
            return
        self._write(record)
        if self.follow_check.isChecked():
            self.view.moveCursor(QTextCursor.MoveOperation.End)

    def _write(self, record: dict) -> None:
        color = LEVEL_COLORS.get(record.get("level", "INFO"), "#c8cdd6")
        logger = record.get("logger", "").replace("app.", "")
        text = (
            f"<span style='color:{color}'>[{record.get('time', '')}] "
            f"{record.get('level', ''):<8}</span> "
            f"<span style='color:#7f8b9c'>{logger}</span>  "
            f"{_escape(record.get('message', ''))}"
        )
        self.view.appendHtml(text)

    def _clear(self) -> None:
        get_buffer().clear()
        self.view.clear()
        self.status.setText("Buffer cleared.")

    def _save(self) -> None:
        path, _f = QFileDialog.getSaveFileName(self, "Save the log", "ains-log.txt", "Text (*.txt)")
        if not path:
            return

        def action() -> None:
            target = dump_records(get_buffer().records(), Path(path))
            self.status.setText(f"Saved to {target}")

        run_guarded(self, "Save the log", action)

    def _open_folder(self) -> None:
        from app.ui.pages.preview import _open_path

        _open_path(self.app.paths.logs)

    # ---------------------------------------------------------------- jobs
    def _reload_jobs(self) -> None:
        jobs = self.app.jobs.all()[-120:]
        self.jobs.fill(
            [
                [
                    job.name,
                    job.lane.value,
                    job.state.value,
                    f"{job.progress * 100:.0f}%",
                    (job.message or (job.error.message if job.error else ""))[:80],
                    f"{job.duration:.1f}s",
                ]
                for job in jobs
            ]
        )


def _escape(text: str) -> str:
    """Escape a log message so it is safe inside the rich-text view."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ⏎ ")
