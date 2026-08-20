"""The desktop interface.

Runs against Qt's offscreen platform so the whole window can be built,
navigated and driven without a display.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.gui

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 is not installed")


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app, application, project):
    from app.ui.main_window import MainWindow
    from app.ui.theme import apply_theme

    apply_theme(qt_app, "dark", "en")
    window = MainWindow(application)
    window.project_opened(project)
    yield window
    window.close()


def test_every_page_builds_and_refreshes(window, qt_app):
    assert window.nav.count() == len(window.pages)
    for row, page in enumerate(window.pages):
        window.nav.setCurrentRow(row)
        qt_app.processEvents()
        page.refresh()
        qt_app.processEvents()
        assert window.stack.currentIndex() == row


def test_the_expected_pages_are_present(window):
    titles = {page.title for page in window.pages}
    for expected in (
        "Dashboard",
        "Projects",
        "New Project",
        "Content",
        "Assets",
        "Templates",
        "Layout",
        "Preview",
        "Export",
        "AI Settings",
        "Adobe Settings",
        "Logs",
    ):
        assert expected in titles


def test_the_dashboard_shows_the_project_metrics(window, qt_app):
    window.show_page("Dashboard")
    qt_app.processEvents()
    dashboard = window.pages[0]
    dashboard.refresh()
    assert int(dashboard.metrics["articles"].value_label.text()) == 5
    assert int(dashboard.metrics["assets"].value_label.text()) == 3


def test_the_content_page_lists_the_imported_stories(window, qt_app):
    window.show_page("Content")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Content")
    assert page.table.rowCount() == 5


def test_editing_a_story_through_the_page_persists(window, qt_app, project):
    window.show_page("Content")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Content")
    page.table.selectRow(0)
    qt_app.processEvents()
    page.title_edit.setText("تیتر ویرایش‌شده")
    page._save()
    with project.uow() as uow:
        titles = [a.title for a in uow.articles.for_project(project.project_id)]
    assert "تیتر ویرایش‌شده" in titles


def test_the_assets_page_lists_the_pictures(window, qt_app):
    window.show_page("Assets")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Assets")
    assert page.table.rowCount() == 3


def test_the_templates_page_shows_the_catalogue(window, qt_app):
    window.show_page("Templates")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Templates")
    assert page.table.rowCount() >= 3


def test_diagnostics_run_from_the_page(window, qt_app):
    """The checks run on a worker thread, so the window never blocks."""
    window.show_page("Diagnostics")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Diagnostics")
    task = page._run(deep=False)
    assert task is not None
    task.wait(60_000)
    qt_app.processEvents()
    assert page.table.rowCount() > 5


def test_after_a_run_the_preview_and_layout_pages_have_content(window, qt_app, application, project):
    application.pipeline.run(project, mode="auto")

    window.show_page("Layout")
    qt_app.processEvents()
    layout_page = next(p for p in window.pages if p.title == "Layout")
    layout_page.refresh()
    assert layout_page.plan is not None
    assert layout_page.tree.topLevelItemCount() == len(layout_page.plan.pages)

    window.show_page("Preview")
    qt_app.processEvents()
    preview_page = next(p for p in window.pages if p.title == "Preview")
    preview_page.refresh()
    qt_app.processEvents()
    assert preview_page.page_list.count() == len(layout_page.plan.pages)

    window.show_page("Export")
    qt_app.processEvents()
    export_page = next(p for p in window.pages if p.title == "Export")
    export_page.refresh()
    assert export_page.output_list.count() > 0


def test_manual_frame_edit_is_undoable(window, qt_app, application, project):
    application.pipeline.run(project, mode="auto")
    window.show_page("Layout")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Layout")
    page.refresh()

    tree_page = page.tree.topLevelItem(0)
    tree_page.setExpanded(True)
    child = None
    for index in range(tree_page.childCount()):
        candidate = tree_page.child(index)
        data = candidate.data(0, 0x0100)
        element = page.plan.page(data[1]).element(data[2])
        if element is not None and not element.locked and element.is_text:
            child = candidate
            break
    assert child is not None

    page.tree.setCurrentItem(child)
    qt_app.processEvents()
    element_id = child.data(0, 0x0100)[2]
    element = page.plan.pages[0].element(element_id)
    before = element.rect.y

    page.y_spin.setValue(before + 1.0)
    page._apply()
    assert element.rect.y == pytest.approx(before + 1.0)
    assert application.undo.can_undo
    application.undo.undo()
    assert element.rect.y == pytest.approx(before)


def test_long_operations_do_not_block_the_window(window, qt_app, project, tmp_path):
    """An import must run on a worker thread, not in the Qt slot."""
    import time

    page = next(p for p in window.pages if p.title == "Content")
    window.show_page("Content")
    qt_app.processEvents()

    sources = []
    for index in range(6):
        path = tmp_path / f"extra_{index}.txt"
        path.write_text(f"title: خبر {index}\n\n" + ("متن خبر. " * 400), encoding="utf-8")
        sources.append(path)

    started = time.monotonic()
    page._import_paths(sources)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"the Qt slot blocked for {elapsed:.2f}s"

    assert page.tasks.busy("import-content") or page.table.rowCount() > 5
    page.tasks.wait_all(60_000)
    qt_app.processEvents()
    page.refresh()
    assert page.table.rowCount() == 11


def test_a_failing_background_task_is_reported_not_raised(window, qt_app, monkeypatch):
    """A worker failure must reach the page, never the Qt event loop."""
    from app.ui.widgets import common

    page = next(p for p in window.pages if p.title == "Content")
    reported: list[str] = []
    monkeypatch.setattr(
        common, "show_error", lambda parent, title, message, detail="": reported.append(message)
    )

    task = page.run_background(
        "boom", "Deliberate failure", lambda: 1 / 0, status=page.status
    )
    assert task is not None
    task.wait(10_000)
    qt_app.processEvents()
    assert reported and "division by zero" in reported[0]
    assert "failed" in page.status.text().lower()


def test_the_event_bridge_forwards_core_events(qt_app, application):
    from app.core.events import EventType
    from app.ui.bridge import EventBridge

    bridge = EventBridge(application.bus)
    received: list[dict] = []
    bridge.qa_report.connect(received.append)
    application.bus.publish(EventType.QA_REPORT, page=3, score=88.0)
    qt_app.processEvents()
    assert received and received[0]["page"] == 3
    bridge.close()


def test_resume_button_appears_for_an_interrupted_run(window, qt_app, application, project):
    from app.models import entities as E

    dashboard = window.pages[0]
    dashboard.refresh()
    # isVisible() is False for every widget while the window itself is hidden,
    # so the explicit hidden flag is what the assertion has to look at.
    assert dashboard.resume_button.isHidden()

    with project.uow() as uow:
        uow.runs.add(E.PipelineRun(project_id=project.project_id, status="running", stage="layout"))
    dashboard.refresh()
    qt_app.processEvents()
    assert not dashboard.resume_button.isHidden()
    assert application.resumable_stage() is not None


def test_a_project_log_file_is_written(application, project):
    logging_target = project.logs_dir / "project.log"
    import logging

    logging.getLogger("app.test").warning("a line for the project log")
    assert logging_target.exists()
    assert "project log" in logging_target.read_text(encoding="utf-8", errors="replace")
