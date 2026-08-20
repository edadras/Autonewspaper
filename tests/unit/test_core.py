"""Errors, events, jobs, undo and versioning."""

from __future__ import annotations

import threading

import pytest

from app.core.errors import Component, LayoutError, Severity, to_report
from app.core.events import EventBus, EventType
from app.core.jobs import JobLane, JobQueue, JobState
from app.core.undo import Command, UndoStack
from app.core.versioning import VersionManager


def test_error_report_carries_context_and_recovery():
    error = LayoutError("cannot place", context={"page": 3})
    report = error.report()
    assert report.component is Component.LAYOUT
    assert report.severity is Severity.ERROR
    assert report.recovery_action
    assert report.context["page"] == 3
    assert "cannot place" in report.summary()


def test_any_exception_converts_to_a_report():
    report = to_report(ValueError("boom"), Component.AI)
    assert report.error_type == "ValueError"
    assert report.component is Component.AI
    assert "boom" in report.message


def test_event_bus_delivers_and_survives_a_broken_listener():
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.QA_REPORT, lambda e: seen.append(e.payload["page"]))
    bus.subscribe(EventType.QA_REPORT, lambda e: (_ for _ in ()).throw(RuntimeError("bad")))
    bus.publish(EventType.QA_REPORT, page=2)
    assert seen == [2]


def test_job_queue_runs_and_reports_progress():
    bus = EventBus()
    queue = JobQueue(bus, workers=2)
    try:
        job = queue.submit("double", lambda ctx: (ctx.progress(0.5, "half"), 21 * 2)[1])
        queue.wait([job], timeout=10)
        assert job.state is JobState.SUCCEEDED
        assert job.result == 42
    finally:
        queue.shutdown()


def test_failing_job_is_captured_not_raised():
    queue = JobQueue(EventBus(), workers=1)
    try:
        job = queue.submit("bad", lambda ctx: 1 / 0)
        queue.wait([job], timeout=10)
        assert job.state is JobState.FAILED
        assert job.error is not None
        assert "ZeroDivisionError" in job.error.error_type
    finally:
        queue.shutdown()


def test_exclusive_jobs_never_overlap():
    queue = JobQueue(EventBus(), workers=4)
    active = {"count": 0, "max": 0}
    lock = threading.Lock()

    def body(_ctx):
        with lock:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        threading.Event().wait(0.05)
        with lock:
            active["count"] -= 1
        return True

    try:
        jobs = [queue.submit(f"x{i}", body, lane=JobLane.EXCLUSIVE) for i in range(4)]
        queue.wait(jobs, timeout=20)
        assert active["max"] == 1
    finally:
        queue.shutdown()


def test_undo_stack_supports_transactions_and_limits():
    stack = UndoStack(limit=3)
    values: list[int] = []
    with stack.transaction("compound"):
        stack.push(Command("a", lambda: values.append(1), lambda: values.pop()))
        stack.push(Command("b", lambda: values.append(2), lambda: values.pop()))
    assert values == [1, 2]
    assert stack.undo() == "compound"
    assert values == []
    assert stack.redo() == "compound"
    assert values == [1, 2]

    for index in range(5):
        stack.do(f"step{index}", lambda: None, lambda: None)
    assert len(stack.history()) == 3


def test_undo_rolls_back_a_failed_transaction():
    stack = UndoStack()
    values: list[int] = []
    with pytest.raises(RuntimeError):
        with stack.transaction("failing"):
            stack.push(Command("a", lambda: values.append(1), lambda: values.pop()))
            raise RuntimeError("stop")
    assert values == []
    assert not stack.can_undo


def test_version_manager_snapshots_and_restores(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text('{"v": 1}', encoding="utf-8")
    manager = VersionManager(project)

    first = manager.create("first")
    (project / "project.json").write_text('{"v": 2}', encoding="utf-8")
    manager.create("second")
    assert len(manager.list()) == 2

    manager.restore(first.number)
    assert (project / "project.json").read_text(encoding="utf-8") == '{"v": 1}'
    assert len(manager.list()) == 3  # the restore snapshots the current state first
