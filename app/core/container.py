"""Dependency injection container.

Components never import each other's singletons; they receive what they need
from the :class:`ServiceContainer`. Registrations are lazy so that, for
example, the Adobe layer is only constructed when a pipeline actually runs.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

log = logging.getLogger(__name__)

T = TypeVar("T")


class ServiceContainer:
    """A minimal, thread-safe service locator with lazy factories.

    ``register`` stores an eagerly created instance, ``register_factory``
    stores a callable that receives the container and builds the instance the
    first time it is requested.
    """

    def __init__(self) -> None:
        self._instances: dict[str, Any] = {}
        self._factories: dict[str, Callable[[ServiceContainer], Any]] = {}
        self._building: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _key(token: type | str) -> str:
        return token if isinstance(token, str) else f"{token.__module__}.{token.__qualname__}"

    def register(self, token: type[T] | str, instance: T) -> T:
        """Register an existing *instance* under *token*."""
        with self._lock:
            self._instances[self._key(token)] = instance
        return instance

    def register_factory(self, token: type[T] | str, factory: Callable[[ServiceContainer], T]) -> None:
        """Register a lazy *factory* for *token*."""
        with self._lock:
            self._factories[self._key(token)] = factory

    def has(self, token: type | str) -> bool:
        """Whether *token* can be resolved."""
        key = self._key(token)
        with self._lock:
            return key in self._instances or key in self._factories

    def resolve(self, token: type[T] | str) -> T:
        """Return the instance registered for *token*, building it if needed."""
        key = self._key(token)
        with self._lock:
            if key in self._instances:
                return cast(T, self._instances[key])
            factory = self._factories.get(key)
            if factory is None:
                raise KeyError(f"No service registered for {key}")
            if key in self._building:
                raise RuntimeError(f"Circular dependency while building {key}")
            self._building.add(key)
        try:
            instance = factory(self)
        finally:
            with self._lock:
                self._building.discard(key)
        with self._lock:
            self._instances[key] = instance
        log.debug("Constructed service %s", key)
        return cast(T, instance)

    def try_resolve(self, token: type[T] | str, default: T | None = None) -> T | None:
        """Like :meth:`resolve` but returns *default* on failure."""
        try:
            return self.resolve(token)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not resolve %s: %s", self._key(token), exc)
            return default

    def replace(self, token: type[T] | str, instance: T) -> None:
        """Swap an already-built instance (used by tests and settings changes)."""
        with self._lock:
            self._instances[self._key(token)] = instance

    def invalidate(self, token: type | str) -> None:
        """Drop a cached instance so that its factory runs again."""
        with self._lock:
            self._instances.pop(self._key(token), None)

    def dispose(self) -> None:
        """Call ``close``/``shutdown`` on every built instance and clear."""
        with self._lock:
            instances = list(self._instances.values())
            self._instances.clear()
        for instance in instances:
            for method in ("shutdown", "close", "dispose"):
                fn = getattr(instance, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # pragma: no cover
                        log.exception("Error disposing %r", instance)
                    break
