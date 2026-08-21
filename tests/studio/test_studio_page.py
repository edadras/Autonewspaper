"""The Studio page, driven the way an operator drives it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 is not installed")


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(qt_app, application):
    from app.ui.bridge import EventBridge
    from app.ui.pages.studio import StudioPage

    widget = StudioPage(application, EventBridge(application.bus))
    yield widget
    widget.tasks.wait_all()
    widget.close()


def _fill(page, **fields: str) -> None:
    page.title_edit.setText(fields.get("title", "job"))
    page.request_edit.setPlainText(fields.get("request", ""))
    page.content_edit.setPlainText(fields.get("content", ""))
    page.format_edit.setText(fields.get("format", ""))
    page.language_edit.setText(fields.get("language", "fa"))


def test_an_empty_brief_is_refused_rather_than_run(page, qt_app) -> None:
    _fill(page)

    page._run()
    qt_app.processEvents()

    assert "Say what you want" in page.status.text()
    assert page.last_run is None


def test_a_missing_size_shows_a_card_with_a_recommendation(page, qt_app) -> None:
    _fill(page, request="یک پوستر برای کنسرت", content="کنسرت بهار")

    page._run()
    page.tasks.wait_all()
    qt_app.processEvents()

    assert page.questions_card.isVisibleTo(page)
    assert page._question_widgets
    question, group = page._question_widgets[0]
    assert question.id == "format"
    assert group.checkedButton() is not None, "the recommendation is chosen for the operator"
    assert not page.files_list.count(), "nothing was made while it was waiting"


def test_answering_the_card_carries_the_work_through(page, qt_app) -> None:
    _fill(page, request="یک پوستر برای کنسرت", content="کنسرت بهار\n\nتالار وحدت")
    page._run()
    page.tasks.wait_all()
    qt_app.processEvents()
    assert page.questions_card.isVisibleTo(page)

    page._answer()
    page.tasks.wait_all()
    qt_app.processEvents()

    assert page.answers.get("format")
    assert page.format_edit.text(), "the answer filled the size in"
    assert page.last_run is not None
    assert page.last_run.finished, page.last_run.stop_reason
    assert page.files_list.count() > 0
    for row in range(page.files_list.count()):
        from PySide6.QtCore import Qt

        path = page.files_list.item(row).data(Qt.ItemDataRole.UserRole)
        assert Path(path).exists()


def test_the_crew_is_listed_with_what_each_specialist_did(page, qt_app) -> None:
    _fill(page, request="یک پوستر", content="سرخط پوستر", format="A4")

    page._run()
    page.tasks.wait_all()
    qt_app.processEvents()

    assert page.crew_list.count() > 0
    text = "\n".join(page.crew_list.item(row).text() for row in range(page.crew_list.count()))
    assert "photoshop" in text
    assert "✓" in text
    assert "concept" not in page.concept_label.text().lower() or page.concept_label.text()


def test_ticking_an_application_overrules_the_router(page, qt_app) -> None:
    _fill(page, request="یک پوستر", content="سرخط", format="A4")
    page.host_checks["indesign"].setChecked(True)

    brief = page._brief()

    assert brief.hosts == ["indesign"]


def test_the_size_is_read_out_of_the_option_label() -> None:
    from app.ui.pages.studio import _size_from

    assert _size_from("A3, 297x420 mm") == "297x420 mm"
    assert _size_from("Vertical, 1080x1920") == "1080x1920"
    assert _size_from("A4") == "A4"
