"""Repositories.

Thin, typed query objects on top of a SQLAlchemy :class:`Session`. Services
depend on repositories rather than on raw queries so that the persistence
details stay in one place.
"""

from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models import entities as E  # noqa: N812

log = logging.getLogger(__name__)

M = TypeVar("M", bound=Base)


class Repository(Generic[M]):
    """Generic CRUD helper."""

    model: type[M]

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, instance: M) -> M:
        """Insert *instance* and flush so its primary key is populated."""
        self.session.add(instance)
        self.session.flush()
        return instance

    def add_all(self, instances: list[M]) -> list[M]:
        """Insert many instances in one flush."""
        self.session.add_all(instances)
        self.session.flush()
        return instances

    def get(self, pk: Any) -> M | None:
        """Fetch by primary key."""
        return self.session.get(self.model, pk)

    def all(self) -> list[M]:
        """Every row."""
        return list(self.session.scalars(select(self.model)).all())

    def count(self) -> int:
        """Number of rows."""
        return int(self.session.scalar(select(func.count()).select_from(self.model)) or 0)

    def delete(self, instance: M) -> None:
        """Delete a single row."""
        self.session.delete(instance)
        self.session.flush()

    def delete_where(self, *conditions: Any) -> int:
        """Bulk delete; returns the number of removed rows."""
        result = self.session.execute(delete(self.model).where(*conditions))
        self.session.flush()
        return int(result.rowcount or 0)


class ProjectRepository(Repository[E.Project]):
    """Queries for :class:`~app.models.entities.Project`."""

    model = E.Project

    def by_slug(self, slug: str) -> E.Project | None:
        """Fetch a project by its directory slug."""
        return self.session.scalar(select(E.Project).where(E.Project.slug == slug))

    def current(self) -> E.Project | None:
        """The project stored in this (per-project) database."""
        return self.session.scalars(select(E.Project).limit(1)).first()


class ArticleRepository(Repository[E.Article]):
    """Queries for :class:`~app.models.entities.Article`."""

    model = E.Article

    def for_project(self, project_id: int) -> list[E.Article]:
        """All articles ordered by editorial order."""
        return list(
            self.session.scalars(
                select(E.Article)
                .where(E.Article.project_id == project_id)
                .order_by(E.Article.order_index, E.Article.id)
            ).all()
        )

    def by_priority(self, project_id: int) -> list[E.Article]:
        """Articles sorted by descending priority then importance."""
        return list(
            self.session.scalars(
                select(E.Article)
                .where(E.Article.project_id == project_id)
                .order_by(E.Article.priority.desc(), E.Article.importance.desc(), E.Article.id)
            ).all()
        )

    def for_page(self, project_id: int, page: int) -> list[E.Article]:
        """Articles the editorial agent assigned to *page*."""
        return list(
            self.session.scalars(
                select(E.Article)
                .where(E.Article.project_id == project_id, E.Article.recommended_page == page)
                .order_by(E.Article.priority.desc())
            ).all()
        )

    def categories(self, project_id: int) -> list[str]:
        """Distinct categories present in the project."""
        rows = self.session.scalars(
            select(E.Article.category).where(E.Article.project_id == project_id).distinct()
        ).all()
        return sorted({row for row in rows if row})

    def next_order_index(self, project_id: int) -> int:
        """Order index to use for a newly imported article."""
        value = self.session.scalar(
            select(func.max(E.Article.order_index)).where(E.Article.project_id == project_id)
        )
        return int(value or 0) + 1


class AssetRepository(Repository[E.Asset]):
    """Queries for :class:`~app.models.entities.Asset`."""

    model = E.Asset

    def for_project(self, project_id: int) -> list[E.Asset]:
        """Every asset of the project."""
        return list(
            self.session.scalars(
                select(E.Asset).where(E.Asset.project_id == project_id).order_by(E.Asset.id)
            ).all()
        )

    def for_article(self, article_id: int) -> list[E.Asset]:
        """Assets linked to an article, best quality first."""
        return list(
            self.session.scalars(
                select(E.Asset)
                .where(E.Asset.article_id == article_id)
                .order_by(E.Asset.quality_score.desc())
            ).all()
        )

    def unassigned(self, project_id: int) -> list[E.Asset]:
        """Images that are not yet linked to an article."""
        return list(
            self.session.scalars(
                select(E.Asset).where(
                    E.Asset.project_id == project_id,
                    E.Asset.article_id.is_(None),
                    E.Asset.type == "image",
                )
            ).all()
        )

    def by_checksum(self, project_id: int, checksum: str) -> E.Asset | None:
        """Find an already-imported identical file."""
        return self.session.scalar(
            select(E.Asset).where(E.Asset.project_id == project_id, E.Asset.checksum == checksum)
        )

    def by_type(self, project_id: int, asset_type: str) -> list[E.Asset]:
        """Assets of a given type (``image``/``logo``/``advertisement``)."""
        return list(
            self.session.scalars(
                select(E.Asset).where(E.Asset.project_id == project_id, E.Asset.type == asset_type)
            ).all()
        )


class PageRepository(Repository[E.Page]):
    """Queries for :class:`~app.models.entities.Page`."""

    model = E.Page

    def for_project(self, project_id: int) -> list[E.Page]:
        """Pages ordered by index."""
        return list(
            self.session.scalars(
                select(E.Page).where(E.Page.project_id == project_id).order_by(E.Page.index)
            ).all()
        )

    def by_index(self, project_id: int, index: int) -> E.Page | None:
        """Fetch a page by its 1-based index."""
        return self.session.scalar(
            select(E.Page).where(E.Page.project_id == project_id, E.Page.index == index)
        )

    def clear(self, project_id: int) -> int:
        """Delete every page (and cascade its elements)."""
        pages = self.for_project(project_id)
        for page in pages:
            self.session.delete(page)
        self.session.flush()
        return len(pages)


class LayoutElementRepository(Repository[E.LayoutElement]):
    """Queries for :class:`~app.models.entities.LayoutElement`."""

    model = E.LayoutElement

    def for_page(self, page_id: int) -> list[E.LayoutElement]:
        """Elements of a page in painting order."""
        return list(
            self.session.scalars(
                select(E.LayoutElement)
                .where(E.LayoutElement.page_id == page_id)
                .order_by(E.LayoutElement.z_index, E.LayoutElement.id)
            ).all()
        )

    def overflowing(self, page_id: int) -> list[E.LayoutElement]:
        """Elements whose text does not fit."""
        return list(
            self.session.scalars(
                select(E.LayoutElement).where(
                    E.LayoutElement.page_id == page_id, E.LayoutElement.overflow.is_(True)
                )
            ).all()
        )


class PipelineRunRepository(Repository[E.PipelineRun]):
    """Queries for :class:`~app.models.entities.PipelineRun`."""

    model = E.PipelineRun

    def latest(self, project_id: int) -> E.PipelineRun | None:
        """Most recent run for the project."""
        return self.session.scalar(
            select(E.PipelineRun)
            .where(E.PipelineRun.project_id == project_id)
            .order_by(E.PipelineRun.id.desc())
            .limit(1)
        )

    def resumable(self, project_id: int) -> E.PipelineRun | None:
        """The most recent run that was interrupted mid-flight."""
        run = self.latest(project_id)
        if run and run.status in {"running", "paused", "awaiting_approval"}:
            return run
        return None


class LogRepository(Repository[E.LogEntry]):
    """Queries for :class:`~app.models.entities.LogEntry`."""

    model = E.LogEntry

    def recent(self, limit: int = 500, level: str | None = None) -> list[E.LogEntry]:
        """Latest log lines, optionally filtered by level."""
        stmt = select(E.LogEntry).order_by(E.LogEntry.id.desc()).limit(limit)
        if level:
            stmt = stmt.where(E.LogEntry.level == level)
        return list(reversed(list(self.session.scalars(stmt).all())))

    def write(self, level: str, message: str, component: str = "core", **detail: Any) -> E.LogEntry:
        """Append a structured log line."""
        entry = E.LogEntry(
            level=level,
            message=message,
            component=component,
            logger=detail.pop("logger", ""),
            detail_json=E.JSONMixin.dump(detail) if detail else "",
        )
        return self.add(entry)


class TemplateRepository(Repository[E.TemplateRecord]):
    """Queries for :class:`~app.models.entities.TemplateRecord`."""

    model = E.TemplateRecord

    def by_template_id(self, template_id: str) -> E.TemplateRecord | None:
        """Fetch by the stable string id used in settings and projects."""
        return self.session.scalar(
            select(E.TemplateRecord).where(E.TemplateRecord.template_id == template_id)
        )

    def upsert(self, record: E.TemplateRecord) -> E.TemplateRecord:
        """Insert or update the template identified by ``template_id``."""
        existing = self.by_template_id(record.template_id)
        if existing is None:
            return self.add(record)
        for column in ("name", "product_type", "language", "builtin", "path", "data_json"):
            setattr(existing, column, getattr(record, column))
        self.session.flush()
        return existing


class UnitOfWork:
    """Bundle of repositories bound to one session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.projects = ProjectRepository(session)
        self.articles = ArticleRepository(session)
        self.assets = AssetRepository(session)
        self.pages = PageRepository(session)
        self.elements = LayoutElementRepository(session)
        self.runs = PipelineRunRepository(session)
        self.logs = LogRepository(session)
        self.templates = TemplateRepository(session)

    def commit(self) -> None:
        """Commit the underlying session."""
        self.session.commit()

    def flush(self) -> None:
        """Flush pending changes without committing."""
        self.session.flush()
