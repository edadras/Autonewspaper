"""ORM entities.

The whole editorial and layout state of a project lives in these tables. The
pipeline persists after every stage so that a crash can be resumed from the
last completed step.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, utcnow


class JSONMixin:
    """Helpers for the ``*_json`` text columns used to store free-form data."""

    @staticmethod
    def load(raw: str | None, default: Any = None) -> Any:
        """Parse a JSON column, returning *default* when empty or invalid."""
        if not raw:
            return default if default is not None else {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default if default is not None else {}

    @staticmethod
    def dump(value: Any) -> str:
        """Serialise *value* for storage."""
        return json.dumps(value, ensure_ascii=False, default=str)


class Project(Base, TimestampMixin, JSONMixin):
    """A single newspaper / magazine edition."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    publication_name: Mapped[str] = mapped_column(String(200), default="")
    edition_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="fa")
    product_type: Mapped[str] = mapped_column(String(32), default="newspaper")
    page_size: Mapped[str] = mapped_column(String(32), default="Broadsheet")
    page_width_mm: Mapped[float] = mapped_column(Float, default=297.0)
    page_height_mm: Mapped[float] = mapped_column(Float, default=420.0)
    page_count: Mapped[int] = mapped_column(Integer, default=8)
    template_id: Mapped[str] = mapped_column(String(96), default="broadsheet_fa_standard")
    design_style: Mapped[str] = mapped_column(String(64), default="classic")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    directory: Mapped[str] = mapped_column(Text, default="")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")

    articles: Mapped[list[Article]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Article.order_index"
    )
    assets: Mapped[list[Asset]] = relationship(back_populates="project", cascade="all, delete-orphan")
    pages: Mapped[list[Page]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Page.index"
    )
    runs: Mapped[list[PipelineRun]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="PipelineRun.id"
    )

    @property
    def settings(self) -> dict[str, Any]:
        """Project-scoped overrides of the global settings."""
        return self.load(self.settings_json)

    def set_settings(self, value: dict[str, Any]) -> None:
        """Replace the project-scoped settings."""
        self.settings_json = self.dump(value)


class Article(Base, TimestampMixin, JSONMixin):
    """One editorial item."""

    __tablename__ = "articles"
    __table_args__ = (Index("ix_articles_project_priority", "project_id", "priority"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)

    title: Mapped[str] = mapped_column(Text, default="")
    original_title: Mapped[str] = mapped_column(Text, default="")
    subtitle: Mapped[str] = mapped_column(Text, default="")
    lead: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    author: Mapped[str] = mapped_column(String(160), default="")
    source: Mapped[str] = mapped_column(String(240), default="")
    language: Mapped[str] = mapped_column(String(8), default="fa")

    importance: Mapped[int] = mapped_column(Integer, default=50)
    urgency: Mapped[int] = mapped_column(Integer, default=50)
    public_interest: Mapped[int] = mapped_column(Integer, default=50)
    visual_importance: Mapped[int] = mapped_column(Integer, default=50)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)

    page_preference: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_area: Mapped[str] = mapped_column(String(24), default="secondary")

    image_required: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_image_required: Mapped[bool] = mapped_column(Boolean, default=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="new")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")

    project: Mapped[Project] = relationship(back_populates="articles")
    assets: Mapped[list[Asset]] = relationship(back_populates="article")

    @property
    def meta(self) -> dict[str, Any]:
        """Free-form per-article metadata (AI rationale, keywords, ...)."""
        return self.load(self.meta_json)

    def set_meta(self, value: dict[str, Any]) -> None:
        """Replace the metadata blob."""
        self.meta_json = self.dump(value)

    @property
    def display_title(self) -> str:
        """Headline to typeset (AI headline wins over the imported one)."""
        return self.title or self.original_title


class Asset(Base, TimestampMixin, JSONMixin):
    """An image, logo or advertisement usable by the layout engine."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL"), nullable=True, index=True
    )

    filename: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(24), default="image")
    source: Mapped[str] = mapped_column(String(32), default="import")
    path: Mapped[str] = mapped_column(Text)
    processed_path: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str] = mapped_column(String(64), default="", index=True)

    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    aspect_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    dpi: Mapped[float] = mapped_column(Float, default=72.0)

    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    blur_score: Mapped[float] = mapped_column(Float, default=0.0)
    face_count: Mapped[int] = mapped_column(Integer, default=0)
    orientation: Mapped[str] = mapped_column(String(16), default="landscape")
    duplicate_of: Mapped[int | None] = mapped_column(Integer, nullable=True)

    provider: Mapped[str] = mapped_column(String(48), default="")
    model: Mapped[str] = mapped_column(String(96), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    meta_json: Mapped[str] = mapped_column(Text, default="{}")

    project: Mapped[Project] = relationship(back_populates="assets")
    article: Mapped[Article | None] = relationship(back_populates="assets")

    @property
    def meta(self) -> dict[str, Any]:
        """Provenance and analysis metadata."""
        return self.load(self.meta_json)

    def set_meta(self, value: dict[str, Any]) -> None:
        """Replace the metadata blob."""
        self.meta_json = self.dump(value)

    @property
    def usable_path(self) -> str:
        """Processed derivative when available, otherwise the original."""
        return self.processed_path or self.path


class Page(Base, TimestampMixin, JSONMixin):
    """One physical page of the edition."""

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("project_id", "index", name="page_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(64), default="")
    master: Mapped[str] = mapped_column(String(64), default="A-Master")
    width_mm: Mapped[float] = mapped_column(Float, default=297.0)
    height_mm: Mapped[float] = mapped_column(Float, default=420.0)
    columns: Mapped[int] = mapped_column(Integer, default=6)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    qa_score: Mapped[float] = mapped_column(Float, default=0.0)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    preview_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="planned")
    meta_json: Mapped[str] = mapped_column(Text, default="{}")

    project: Mapped[Project] = relationship(back_populates="pages")
    elements: Mapped[list[LayoutElement]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="LayoutElement.z_index"
    )

    @property
    def meta(self) -> dict[str, Any]:
        """QA issues, chosen candidate id and other per-page data."""
        return self.load(self.meta_json)

    def set_meta(self, value: dict[str, Any]) -> None:
        """Replace the metadata blob."""
        self.meta_json = self.dump(value)


class LayoutElement(Base, TimestampMixin, JSONMixin):
    """A positioned frame on a page (millimetre coordinates)."""

    __tablename__ = "layout_elements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)

    type: Mapped[str] = mapped_column(String(32), default="body")
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[float] = mapped_column(Float, default=0.0)
    height: Mapped[float] = mapped_column(Float, default=0.0)
    z_index: Mapped[int] = mapped_column(Integer, default=0)
    rotation: Mapped[float] = mapped_column(Float, default=0.0)
    column_span: Mapped[int] = mapped_column(Integer, default=1)

    article_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    style_id: Mapped[str] = mapped_column(String(64), default="")
    frame_name: Mapped[str] = mapped_column(String(96), default="")
    text_content: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str] = mapped_column(Text, default="")
    overflow: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")

    page: Mapped[Page] = relationship(back_populates="elements")

    @property
    def meta(self) -> dict[str, Any]:
        """Typography overrides and diagnostics for this frame."""
        return self.load(self.meta_json)

    def set_meta(self, value: dict[str, Any]) -> None:
        """Replace the metadata blob."""
        self.meta_json = self.dump(value)


class PipelineRun(Base, JSONMixin):
    """One execution of the autonomous pipeline (supports crash recovery)."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="auto")
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    stage: Mapped[str] = mapped_column(String(48), default="import")
    completed_stages: Mapped[str] = mapped_column(Text, default="[]")
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_json: Mapped[str] = mapped_column(Text, default="")

    project: Mapped[Project] = relationship(back_populates="runs")

    @property
    def stages_done(self) -> list[str]:
        """Names of the stages that already completed."""
        return self.load(self.completed_stages, [])

    def mark_stage(self, stage: str) -> None:
        """Record *stage* as completed."""
        done = self.stages_done
        if stage not in done:
            done.append(stage)
        self.completed_stages = self.dump(done)

    @property
    def result(self) -> dict[str, Any]:
        """Artifacts produced by the run (paths, scores)."""
        return self.load(self.result_json)

    def set_result(self, value: dict[str, Any]) -> None:
        """Replace the result blob."""
        self.result_json = self.dump(value)


class JobRecord(Base, JSONMixin):
    """Persisted snapshot of a queued job (for the Logs / Jobs page)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(240))
    lane: Mapped[str] = mapped_column(String(16), default="parallel")
    state: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    error_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LogEntry(Base):
    """Structured log line persisted with the project."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(12), default="INFO", index=True)
    logger: Mapped[str] = mapped_column(String(120), default="")
    component: Mapped[str] = mapped_column(String(32), default="core")
    message: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class VersionRecord(Base):
    """Index of the snapshots created by :class:`~app.core.versioning.VersionManager`."""

    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    number: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TemplateRecord(Base, JSONMixin):
    """A registered layout template."""

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    product_type: Mapped[str] = mapped_column(String(32), default="newspaper")
    language: Mapped[str] = mapped_column(String(8), default="fa")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    path: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SettingRecord(Base):
    """Key/value store for values that belong to the database rather than to
    ``settings.json`` (last opened project, window geometry, ...)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


ALL_ENTITIES = [
    Project,
    Article,
    Asset,
    Page,
    LayoutElement,
    PipelineRun,
    JobRecord,
    LogEntry,
    VersionRecord,
    TemplateRecord,
    SettingRecord,
]
