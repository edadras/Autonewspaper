"""Job queue and worker pool.

Long running work never blocks the UI thread. Jobs are submitted to a
:class:`JobQueue`, executed by a bounded thread pool, and report their state
through the :class:`~app.core.events.EventBus`.

Two execution lanes exist:

``parallel``
    Independent work (image analysis, AI calls, downloads, OCR).
``exclusive``
    Anything that touches a shared Adobe document. Exclusive jobs are
    serialised against each other regardless of the pool size, because
    InDesign's scripting DOM is single-document/single-threaded.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from app.core.errors import Component, ErrorReport, to_report
from app.core.events import EventBus, EventType

log = logging.getLogger(__name__)

T = TypeVar("T")


class JobState(str, Enum):
    """Life-cycle of a job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobLane(str, Enum):
    """Execution lane, see module docstring."""

    PARALLEL = "parallel"
    EXCLUSIVE = "exclusive"


class CancelToken:
    """Cooperative cancellation flag shared with a running job."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation was requested."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`JobCancelled` when cancellation was requested."""
        if self._event.is_set():
            raise JobCancelled("Operation cancelled by user")

    def wait(self, timeout: float) -> bool:
        """Sleep up to *timeout* seconds, returning early on cancellation."""
        return self._event.wait(timeout)


class JobCancelled(Exception):
    """Raised inside a job body when the user cancels it."""


@dataclass
class JobContext:
    """Handed to every job body so it can report progress and cancel."""

    job_id: str
    token: CancelToken
    bus: EventBus
    _progress: Callable[[float, str], None]

    def progress(self, fraction: float, message: str = "") -> None:
        """Report progress in the ``0.0 .. 1.0`` range."""
        self.token.raise_if_cancelled()
        self._progress(max(0.0, min(1.0, fraction)), message)

    def check(self) -> None:
        """Raise if the job has been cancelled."""
        self.token.raise_if_cancelled()


@dataclass
class Job(Generic[T]):
    """A unit of queued work."""

    name: str
    func: Callable[[JobContext], T]
    lane: JobLane = JobLane.PARALLEL
    priority: int = 50
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: ErrorReport | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    token: CancelToken = field(default_factory=CancelToken)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        """Elapsed seconds (running or final)."""
        if not self.started_at:
            return 0.0
        end = self.finished_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()

    def cancel(self) -> None:
        """Request cancellation of this job."""
        self.token.cancel()

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly snapshot for the UI and the database."""
        return {
            "job_id": self.job_id,
            "name": self.name,
            "lane": self.lane.value,
            "state": self.state.value,
            "progress": round(self.progress, 4),
            "message": self.message,
            "duration": round(self.duration, 3),
            "error": self.error.to_dict() if self.error else None,
            "metadata": self.metadata,
        }


class JobQueue:
    """Bounded worker pool with a serialised exclusive lane.

    Parameters
    ----------
    bus:
        Event bus used to broadcast job state changes.
    workers:
        Size of the parallel pool.
    """

    def __init__(self, bus: EventBus, workers: int = 4) -> None:
        self.bus = bus
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="ains-job"
        )
        self._exclusive_lock = threading.RLock()
        self._jobs: dict[str, Job[Any]] = {}
        self._futures: dict[str, concurrent.futures.Future[Any]] = {}
        self._lock = threading.RLock()
        self._closed = False

    # ---------------------------------------------------------------- submit
    def submit(
        self,
        name: str,
        func: Callable[[JobContext], T],
        *,
        lane: JobLane = JobLane.PARALLEL,
        priority: int = 50,
        metadata: dict[str, Any] | None = None,
    ) -> Job[T]:
        """Queue *func* and return the :class:`Job` handle immediately."""
        if self._closed:
            raise RuntimeError("JobQueue is closed")
        job: Job[T] = Job(name=name, func=func, lane=lane, priority=priority, metadata=metadata or {})
        with self._lock:
            self._jobs[job.job_id] = job
        self.bus.publish(EventType.JOB_QUEUED, job=job.to_dict())
        future = self._pool.submit(self._run, job)
        with self._lock:
            self._futures[job.job_id] = future
        return job

    def map(
        self,
        name: str,
        items: list[Any],
        func: Callable[[Any, JobContext], T],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> list[Job[T]]:
        """Submit one parallel job per element of *items*."""
        jobs: list[Job[T]] = []
        for index, item in enumerate(items):
            meta = dict(metadata or {})
            meta["index"] = index
            jobs.append(
                self.submit(
                    f"{name} [{index + 1}/{len(items)}]",
                    lambda ctx, _item=item: func(_item, ctx),
                    metadata=meta,
                )
            )
        return jobs

    # ------------------------------------------------------------------ run
    def _run(self, job: Job[Any]) -> Any:
        if job.token.cancelled:
            job.state = JobState.CANCELLED
            job.finished_at = datetime.now(UTC)
            self.bus.publish(EventType.JOB_FINISHED, job=job.to_dict())
            return None

        def _report(fraction: float, message: str) -> None:
            job.progress = fraction
            job.message = message
            self.bus.publish(EventType.JOB_PROGRESS, job=job.to_dict())

        ctx = JobContext(job_id=job.job_id, token=job.token, bus=self.bus, _progress=_report)
        acquired = False
        try:
            if job.lane is JobLane.EXCLUSIVE:
                self._exclusive_lock.acquire()
                acquired = True
            job.state = JobState.RUNNING
            job.started_at = datetime.now(UTC)
            self.bus.publish(EventType.JOB_STARTED, job=job.to_dict())
            result = job.func(ctx)
            job.result = result
            job.state = JobState.SUCCEEDED
            job.progress = 1.0
            self.bus.publish(EventType.JOB_FINISHED, job=job.to_dict())
            return result
        except JobCancelled:
            job.state = JobState.CANCELLED
            log.info("Job cancelled: %s", job.name)
            self.bus.publish(EventType.JOB_FINISHED, job=job.to_dict())
            return None
        except Exception as exc:  # noqa: BLE001 - jobs must never kill the pool
            job.state = JobState.FAILED
            job.error = to_report(exc, Component.CORE)
            log.error("Job failed: %s -> %s", job.name, job.error.summary(), exc_info=exc)
            self.bus.publish(EventType.JOB_FAILED, job=job.to_dict())
            self.bus.publish(EventType.ERROR, error=job.error.to_dict())
            return None
        finally:
            job.finished_at = datetime.now(UTC)
            if acquired:
                self._exclusive_lock.release()

    # ---------------------------------------------------------------- access
    def get(self, job_id: str) -> Job[Any] | None:
        """Return a job by id."""
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> list[Job[Any]]:
        """Jobs that are queued or running."""
        with self._lock:
            return [j for j in self._jobs.values() if j.state in (JobState.QUEUED, JobState.RUNNING)]

    def all(self) -> list[Job[Any]]:
        """Every job known to the queue, newest last."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def wait(self, jobs: list[Job[Any]] | None = None, timeout: float | None = None) -> list[Job[Any]]:
        """Block until *jobs* (default: all) finish, then return them."""
        targets = jobs if jobs is not None else self.all()
        with self._lock:
            futures = [self._futures[j.job_id] for j in targets if j.job_id in self._futures]
        concurrent.futures.wait(futures, timeout=timeout)
        return targets

    def results(self, jobs: list[Job[Any]], timeout: float | None = None) -> list[Any]:
        """Wait for *jobs* and return the successful results in order."""
        self.wait(jobs, timeout=timeout)
        return [j.result for j in jobs if j.state is JobState.SUCCEEDED]

    def cancel_all(self) -> int:
        """Request cancellation for every unfinished job."""
        count = 0
        for job in self.active():
            job.cancel()
            count += 1
        return count

    def prune(self, keep: int = 200) -> None:
        """Drop the oldest finished jobs, keeping the queue bounded."""
        with self._lock:
            finished = [
                j
                for j in sorted(self._jobs.values(), key=lambda j: j.created_at)
                if j.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
            ]
            for job in finished[: max(0, len(finished) - keep)]:
                self._jobs.pop(job.job_id, None)
                self._futures.pop(job.job_id, None)

    def shutdown(self, wait: bool = True, timeout: float = 10.0) -> None:
        """Cancel everything and stop the pool."""
        self._closed = True
        self.cancel_all()
        deadline = time.monotonic() + timeout
        if wait:
            while self.active() and time.monotonic() < deadline:
                time.sleep(0.05)
        self._pool.shutdown(wait=False, cancel_futures=True)
