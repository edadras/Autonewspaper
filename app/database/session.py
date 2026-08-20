"""Engine and session management.

Two databases are used:

``app.sqlite``
    Global registry - known projects, installed templates, UI state.
``projects/<slug>/database.sqlite``
    Everything belonging to a single edition. Keeping the project database
    inside the project folder makes a project directory self-contained and
    portable (and is what the crash-recovery code reads on start-up).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DatabaseError
from app.database.base import Base
from app.models import entities as E  # noqa: N812 - entity module alias

log = logging.getLogger(__name__)


def _configure_sqlite(engine: Engine) -> None:
    """Enable WAL, foreign keys and a sane busy timeout on every connection."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=8000")
        finally:
            cursor.close()


class Database:
    """Owns one SQLAlchemy engine and hands out sessions."""

    def __init__(self, path: Path | str, echo: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.url = f"sqlite:///{self.path.as_posix()}"
        self.engine: Engine = create_engine(
            self.url, echo=echo, future=True, connect_args={"check_same_thread": False}
        )
        _configure_sqlite(self.engine)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.create_all()

    def create_all(self) -> None:
        """Create any missing tables."""
        try:
            Base.metadata.create_all(self.engine)
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"Cannot initialise database {self.path}", cause=exc) from exc

    def session(self) -> Session:
        """Return a new session (caller is responsible for closing it)."""
        return self._session_factory()

    @contextmanager
    def scope(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception as exc:
            session.rollback()
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError(f"Database operation failed: {exc}", cause=exc) from exc
        finally:
            session.close()

    def vacuum(self) -> None:
        """Compact the database file."""
        with self.engine.connect() as connection:
            connection.exec_driver_sql("VACUUM")

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()

    close = dispose


class AppDatabase(Database):
    """Global registry database."""

    def get_setting(self, key: str, default: str = "") -> str:
        """Read a UI/app level setting."""
        with self.scope() as session:
            row = session.get(E.SettingRecord, key)
            return row.value if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Write a UI/app level setting."""
        with self.scope() as session:
            row = session.get(E.SettingRecord, key)
            if row is None:
                session.add(E.SettingRecord(key=key, value=value))
            else:
                row.value = value

    def register_project(self, project: E.Project) -> None:
        """Mirror a project row into the global registry."""
        with self.scope() as session:
            existing = session.scalar(select(E.Project).where(E.Project.slug == project.slug))
            payload = {
                "slug": project.slug,
                "name": project.name,
                "publication_name": project.publication_name,
                "edition_date": project.edition_date,
                "language": project.language,
                "product_type": project.product_type,
                "page_size": project.page_size,
                "page_width_mm": project.page_width_mm,
                "page_height_mm": project.page_height_mm,
                "page_count": project.page_count,
                "template_id": project.template_id,
                "design_style": project.design_style,
                "status": project.status,
                "directory": project.directory,
            }
            if existing is None:
                session.add(E.Project(**payload))
            else:
                for key, value in payload.items():
                    setattr(existing, key, value)

    def list_projects(self) -> list[dict[str, Any]]:
        """Registry entries, newest first."""
        with self.scope() as session:
            rows = session.scalars(select(E.Project).order_by(E.Project.updated_at.desc())).all()
            return [row.to_dict() for row in rows]

    def unregister_project(self, slug: str) -> None:
        """Remove a project from the registry (files are untouched)."""
        with self.scope() as session:
            row = session.scalar(select(E.Project).where(E.Project.slug == slug))
            if row is not None:
                session.delete(row)


class ProjectDatabase(Database):
    """Per-project database."""

    def __init__(self, project_dir: Path | str, echo: bool = False) -> None:
        self.project_dir = Path(project_dir)
        super().__init__(self.project_dir / "database.sqlite", echo=echo)

    def project(self, session: Session) -> E.Project:
        """Return the single :class:`Project` row of this database."""
        row = session.scalars(select(E.Project).limit(1)).first()
        if row is None:
            raise DatabaseError(f"Project database at {self.path} contains no project row")
        return row
