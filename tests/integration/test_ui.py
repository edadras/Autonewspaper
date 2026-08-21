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
        "Studio",
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

    task = page.run_background("boom", "Deliberate failure", lambda: 1 / 0, status=page.status)
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


def test_editing_a_story_is_undoable(window, qt_app, application, project):
    window.show_page("Content")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Content")
    page.refresh()
    page.table.selectRow(0)
    qt_app.processEvents()

    article_id = page._current_id
    with project.uow() as uow:
        original = uow.articles.get(article_id).title

    page.title_edit.setText("تیتر موقت")
    page._save()
    with project.uow() as uow:
        assert uow.articles.get(article_id).title == "تیتر موقت"

    assert application.undo.can_undo
    application.undo.undo()
    with project.uow() as uow:
        assert uow.articles.get(article_id).title == original


def test_deleting_a_story_is_undoable(window, qt_app, application, project, monkeypatch):
    from app.ui.pages import content as content_page

    monkeypatch.setattr(content_page, "confirm", lambda *args, **kwargs: True)
    window.show_page("Content")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Content")
    page.refresh()
    page.table.selectRow(0)
    qt_app.processEvents()

    article_id = page._current_id
    page._delete()
    with project.uow() as uow:
        assert uow.articles.get(article_id) is None

    application.undo.undo()
    with project.uow() as uow:
        assert uow.articles.get(article_id) is not None


def test_assigning_a_picture_is_undoable(window, qt_app, application, project):
    window.show_page("Assets")
    qt_app.processEvents()
    page = next(p for p in window.pages if p.title == "Assets")
    page.refresh()
    page.table.selectRow(0)
    qt_app.processEvents()

    asset_id = page.table.selected_data()
    before = application.assets.snapshot_assignments(project)[asset_id]
    with project.uow() as uow:
        article_id = uow.articles.for_project(project.project_id)[0].id

    index = page.assign_box.findData(article_id)
    assert index >= 0
    page.assign_box.setCurrentIndex(index)
    page._assign()
    assert application.assets.snapshot_assignments(project)[asset_id] == article_id

    application.undo.undo()
    assert application.assets.snapshot_assignments(project)[asset_id] == before


# ------------------------------------------------------- unhandled failures
@pytest.fixture
def reported_errors(window, monkeypatch):
    """Collect what the crash guard reports, without opening a modal dialog."""
    shown: list[tuple] = []
    monkeypatch.setattr(
        "app.ui.widgets.common.show_error",
        lambda parent, title, message, detail="": shown.append((title, message, detail)),
    )
    reports: list = []
    window.crash_guard.reported.connect(reports.append)
    return reports, shown


def test_an_exception_escaping_a_slot_is_reported_not_fatal(qt_app, window, reported_errors):
    """§32: nothing may take the window down, and the report must be complete."""
    import sys

    reports, shown = reported_errors

    try:
        raise ValueError("a slot blew up")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    qt_app.processEvents()

    # The window is still there and still navigable - nothing aborted.
    window.show_page("Dashboard")
    assert window.stack.currentWidget() is not None
    assert len(reports) == 1
    report = reports[0]
    assert report.error_type == "ValueError"
    assert report.message == "a slot blew up"
    assert report.component.value == "ui"
    assert report.timestamp is not None
    assert "ValueError" in (report.stack_trace or "")
    assert shown and "a slot blew up" in shown[0][1]


def test_a_repeating_fault_is_only_shown_once(qt_app, window, reported_errors):
    """A broken paint event fires on every repaint; one dialog is enough."""
    import sys

    reports, _shown = reported_errors

    for _ in range(5):
        try:
            raise RuntimeError("repaint failed")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
    qt_app.processEvents()

    assert len(reports) == 1


def test_a_worker_thread_failure_is_logged_without_a_dialog(qt_app, window, reported_errors, caplog):
    """A background thread must never build a widget."""
    import logging
    import threading

    reports, _shown = reported_errors

    def explode():
        raise OSError("the worker could not write")

    with caplog.at_level(logging.CRITICAL, logger="app.unhandled"):
        thread = threading.Thread(target=explode, name="ains-test-worker")
        thread.start()
        thread.join()
    qt_app.processEvents()

    assert reports == []
    assert any("the worker could not write" in record.getMessage() for record in caplog.records)


def test_walking_every_page_of_a_finished_edition_raises_nothing(
    window, qt_app, application, project, reported_errors, caplog
):
    """The pages are only exercised with content after a run has produced some.

    Building a page against an empty project proves little; the interesting
    state is a finished edition, where every table, preview and export control
    has real data behind it.
    """
    import logging

    application.pipeline.run(project, mode="auto")
    window.project_opened(project)
    reports, _shown = reported_errors

    with caplog.at_level(logging.ERROR):
        for row, page in enumerate(window.pages):
            window.nav.setCurrentRow(row)
            qt_app.processEvents()
            page.refresh()
            qt_app.processEvents()
            page.tasks.wait_all(5000)
            qt_app.processEvents()

    assert reports == [], [report.summary() for report in reports]
    offenders = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.name.startswith("app.ui")
    ]
    assert offenders == []


# ------------------------------------------ choosing the InDesign document
def _page(window, title: str):
    return next(page for page in window.pages if page.title == title)


def test_the_document_source_can_be_chosen_and_saved(window, qt_app):
    """§Adobe settings: the operator decides which document a run builds into."""
    page = _page(window, "Adobe Settings")
    page.refresh()
    qt_app.processEvents()

    values = [page.document_source.itemData(i) for i in range(page.document_source.count())]
    assert values == ["auto", "open_document", "template_file", "new_document"]
    assert page.document_source.currentData() == "auto"

    page.document_source.setCurrentIndex(values.index("new_document"))
    page.adopt_geometry.setChecked(False)
    page._save()
    page.tasks.wait_all(5000)
    qt_app.processEvents()

    adobe = window.app.settings.settings.adobe
    assert adobe.document_source == "new_document"
    assert adobe.adopt_open_geometry is False

    page.refresh()
    qt_app.processEvents()
    assert page.document_source.currentData() == "new_document"


def test_linking_an_indesign_file_copies_a_builtin_template(window, qt_app, monkeypatch, tmp_path):
    """Built-ins cannot be edited, so the link goes onto an editable copy."""
    document = tmp_path / "house-style.indt"
    document.write_bytes(b"stand-in for a real InDesign template")

    page = _page(window, "Templates")
    page.refresh()
    qt_app.processEvents()
    builtin = window.app.settings.settings.default_template_id
    assert window.app.templates.is_builtin(builtin)
    page.table.select_data(builtin)
    qt_app.processEvents()

    monkeypatch.setattr(
        "app.ui.pages.templates.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(document), ""),
    )
    monkeypatch.setattr("app.ui.pages.templates.confirm", lambda *a, **k: True)

    page._link_indesign_document()
    qt_app.processEvents()

    copies = [tid for tid in window.app.templates.ids() if tid.startswith(f"{builtin}_linked")]
    assert copies, "no editable copy was created"
    copy = window.app.templates.get(copies[0])
    assert copy.indesign_template_path == str(document)
    # The built-in itself is untouched.
    assert window.app.templates.get(builtin).indesign_template_path is None
    # And the project now uses the copy, so the link actually takes effect.
    assert page.handle is not None
    assert page.handle.project()["template_id"] == copy.id
