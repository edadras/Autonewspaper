"""Undo / redo support.

Every user-visible mutation is expressed as a :class:`Command` with a
``do``/``undo`` pair. The stack keeps at least 50 steps (configurable) and
supports transactions so that a compound edit is undone atomically.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Command:
    """A reversible operation.

    Parameters
    ----------
    label:
        Text shown in the Undo menu (``"Move image"``).
    do:
        Callable performing the change.
    undo:
        Callable reverting it.
    """

    label: str
    do: Callable[[], Any]
    undo: Callable[[], Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class CompositeCommand(Command):
    """A group of commands executed and reverted as a single unit."""

    def __init__(self, label: str, commands: list[Command]) -> None:
        self.commands = commands
        super().__init__(label=label, do=self._do_all, undo=self._undo_all)

    def _do_all(self) -> None:
        for command in self.commands:
            command.do()

    def _undo_all(self) -> None:
        for command in reversed(self.commands):
            command.undo()


class UndoStack:
    """Bounded undo/redo stack with transaction support."""

    def __init__(self, limit: int = 50) -> None:
        self.limit = max(1, limit)
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._lock = threading.RLock()
        self._transaction: list[Command] | None = None
        self._transaction_label = ""
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------- mutation
    def push(self, command: Command, *, execute: bool = True) -> Any:
        """Execute *command* (unless ``execute=False``) and record it."""
        result = command.do() if execute else None
        with self._lock:
            if self._transaction is not None:
                self._transaction.append(command)
                return result
            self._undo.append(command)
            if len(self._undo) > self.limit:
                del self._undo[0 : len(self._undo) - self.limit]
            self._redo.clear()
        self._notify()
        return result

    def do(self, label: str, do: Callable[[], Any], undo: Callable[[], Any], **metadata: Any) -> Any:
        """Convenience wrapper building and pushing a :class:`Command`."""
        return self.push(Command(label=label, do=do, undo=undo, metadata=metadata))

    def undo(self) -> str | None:
        """Revert the last command; returns its label."""
        with self._lock:
            if not self._undo:
                return None
            command = self._undo.pop()
        try:
            command.undo()
        except Exception:
            log.exception("Undo failed for %s", command.label)
            with self._lock:
                self._undo.append(command)
            raise
        with self._lock:
            self._redo.append(command)
        self._notify()
        return command.label

    def redo(self) -> str | None:
        """Re-apply the last undone command; returns its label."""
        with self._lock:
            if not self._redo:
                return None
            command = self._redo.pop()
        try:
            command.do()
        except Exception:
            log.exception("Redo failed for %s", command.label)
            with self._lock:
                self._redo.append(command)
            raise
        with self._lock:
            self._undo.append(command)
        self._notify()
        return command.label

    # ---------------------------------------------------------- transaction
    def begin(self, label: str) -> None:
        """Start a transaction; nested transactions are flattened."""
        with self._lock:
            if self._transaction is None:
                self._transaction = []
                self._transaction_label = label

    def commit(self) -> None:
        """Close the transaction, recording it as a single undo step."""
        with self._lock:
            commands = self._transaction
            label = self._transaction_label
            self._transaction = None
        if commands:
            self.push(CompositeCommand(label, commands), execute=False)

    def rollback(self) -> None:
        """Abort the transaction, reverting whatever has been applied."""
        with self._lock:
            commands = self._transaction or []
            self._transaction = None
        for command in reversed(commands):
            try:
                command.undo()
            except Exception:  # pragma: no cover
                log.exception("Rollback step failed: %s", command.label)

    class _Transaction:
        def __init__(self, stack: UndoStack, label: str) -> None:
            self.stack = stack
            self.label = label

        def __enter__(self) -> UndoStack:
            self.stack.begin(self.label)
            return self.stack

        def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
            if exc_type is None:
                self.stack.commit()
            else:
                self.stack.rollback()
            return False

    def transaction(self, label: str) -> _Transaction:
        """Context manager form of :meth:`begin`/:meth:`commit`."""
        return UndoStack._Transaction(self, label)

    # -------------------------------------------------------------- queries
    @property
    def can_undo(self) -> bool:
        """Whether an undo step is available."""
        with self._lock:
            return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        """Whether a redo step is available."""
        with self._lock:
            return bool(self._redo)

    def undo_label(self) -> str | None:
        """Label of the next undo step."""
        with self._lock:
            return self._undo[-1].label if self._undo else None

    def redo_label(self) -> str | None:
        """Label of the next redo step."""
        with self._lock:
            return self._redo[-1].label if self._redo else None

    def history(self) -> list[str]:
        """Labels of the recorded undo steps, oldest first."""
        with self._lock:
            return [c.label for c in self._undo]

    def clear(self) -> None:
        """Drop all history."""
        with self._lock:
            self._undo.clear()
            self._redo.clear()
        self._notify()

    # ------------------------------------------------------------ listeners
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a change listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # pragma: no cover
                log.exception("Undo listener failed")
