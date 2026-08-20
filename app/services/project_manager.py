"""Project life-cycle.

A project is a self-contained directory (specification §6) holding its own
manifest, database, content, assets, Adobe artefacts, previews, output and
logs. Everything the application knows about an edition lives there, which is
what makes a project portable and a crash recoverable.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.config.paths import AppPaths
from app.core.errors import AppError, Component, DatabaseError
from app.core.events import EventBus, EventType
from app.core.versioning import VersionManager
from app.database.repositories import UnitOfWork
from app.database.session import AppDatabase, ProjectDatabase
from app.models import entities as E  # noqa: N812
from app.models.schemas import ProjectSpec
from app.utils.files import make_archive, read_json, slugify, write_json
from app.utils.units import page_size

log = logging.getLogger(__name__)

PROJECT_MANIFEST = "project.json"
MANIFEST_VERSION = "1.0"

SUBDIRECTORIES = (
    "content/articles",
    "assets/images",
    "assets/generated",
    "assets/processed",
    "assets/thumbnails",
    "templates",
    "adobe/indesign",
    "adobe/photoshop",
    "adobe/scripts",
    "previews",
    "output/previews",
    "output/assets",
    "output/archive",
    "versions",
    "logs",
)


class ProjectError(AppError):
    """A project could not be created, opened or removed."""

    component = Component.CORE


@dataclass
class ProjectHandle:
    """An open project: its directory, database and helpers."""

    slug: str
    directory: Path
    database: ProjectDatabase
    versions: VersionManager
    project_id: int

    # -- directories ------------------------------------------------------
    @property
    def content_dir(self) -> Path:
        """Imported source documents."""
        return self.directory / "content" / "articles"

    @property
    def images_dir(self) -> Path:
        """Imported images."""
        return self.directory / "assets" / "images"

    @property
    def generated_dir(self) -> Path:
        """AI generated images."""
        return self.directory / "assets" / "generated"

    @property
    def processed_dir(self) -> Path:
        """Processed derivatives ready for placement."""
        return self.directory / "assets" / "processed"

    @property
    def thumbnails_dir(self) -> Path:
        """Asset thumbnails for the UI."""
        return self.directory / "assets" / "thumbnails"

    @property
    def previews_dir(self) -> Path:
        """Page previews produced during QA."""
        return self.directory / "previews"

    @property
    def output_dir(self) -> Path:
        """Final deliverables."""
        return self.directory / "output"

    @property
    def adobe_dir(self) -> Path:
        """InDesign/Photoshop artefacts and generated scripts."""
        return self.directory / "adobe"

    @property
    def logs_dir(self) -> Path:
        """Per-project log files."""
        return self.directory / "logs"

    @property
    def manifest_path(self) -> Path:
        """``project.json``."""
        return self.directory / PROJECT_MANIFEST

    @property
    def layout_plan_path(self) -> Path:
        """Where the current layout plan is persisted."""
        return self.directory / "layout_plan.json"

    @property
    def editorial_plan_path(self) -> Path:
        """Where the current editorial plan is persisted."""
        return self.directory / "editorial_plan.json"

    # -- data -------------------------------------------------------------
    def uow(self):
        """Context manager yielding a :class:`UnitOfWork`."""
        return _UnitOfWorkScope(self.database)

    def project(self) -> dict[str, Any]:
        """The project row as a plain dictionary."""
        with self.database.scope() as session:
            row = session.get(E.Project, self.project_id)
            if row is None:
                raise DatabaseError(f"Project {self.project_id} vanished from its database")
            return row.to_dict()

    def manifest(self) -> dict[str, Any]:
        """Contents of ``project.json``."""
        return read_json(self.manifest_path, {}) or {}

    def close(self) -> None:
        """Release the database connections."""
        self.database.dispose()


class _UnitOfWorkScope:
    """Context manager returning a :class:`UnitOfWork` bound to a session."""

    def __init__(self, database: ProjectDatabase) -> None:
        self._database = database
        self._scope = None
        self._session = None

    def __enter__(self) -> UnitOfWork:
        self._scope = self._database.scope()
        self._session = self._scope.__enter__()
        return UnitOfWork(self._session)

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        assert self._scope is not None
        return bool(self._scope.__exit__(exc_type, exc, tb))


class ProjectManager:
    """Creates, opens, lists, archives and deletes projects."""

    def __init__(self, paths: AppPaths, app_db: AppDatabase, bus: EventBus | None = None) -> None:
        self.paths = paths
        self.app_db = app_db
        self.bus = bus
        self._open: dict[str, ProjectHandle] = {}

    # ------------------------------------------------------------- create
    def create(self, spec: ProjectSpec, *, directory: Path | None = None) -> ProjectHandle:
        """Create a new project directory, database and manifest."""
        slug = self._unique_slug(spec.name)
        target = Path(directory) if directory else self.paths.projects / slug
        if target.exists() and any(target.iterdir()):
            raise ProjectError(
                f"The directory {target} already exists and is not empty",
                recovery_action="Choose another project name or directory.",
            )
        for sub in SUBDIRECTORIES:
            (target / sub).mkdir(parents=True, exist_ok=True)

        width, height = (spec.page_width_mm, spec.page_height_mm)
        if spec.page_size and spec.page_size in ("Broadsheet", "Tabloid", "A4", "A3", "Berliner"):
            width, height = page_size(spec.page_size, (width, height))

        database = ProjectDatabase(target)
        with database.scope() as session:
            row = E.Project(
                slug=slug,
                name=spec.name,
                publication_name=spec.publication_name or spec.name,
                edition_date=spec.edition_date,
                language=spec.language,
                product_type=spec.product_type,
                page_size=spec.page_size,
                page_width_mm=width,
                page_height_mm=height,
                page_count=spec.page_count,
                template_id=spec.template_id,
                design_style=spec.design_style,
                status="draft",
                directory=str(target),
            )
            row.set_settings({"ai_provider": spec.ai_provider})
            session.add(row)
            session.flush()
            project_id = row.id
            self.app_db.register_project(row)

        handle = ProjectHandle(
            slug=slug,
            directory=target,
            database=database,
            versions=VersionManager(target),
            project_id=project_id,
        )
        self._write_manifest(handle, spec)
        self._open[slug] = handle
        log.info("Created project '%s' at %s", spec.name, target)
        self._emit(EventType.PROJECT_CREATED, slug=slug, name=spec.name, directory=str(target))
        return handle

    def _unique_slug(self, name: str) -> str:
        base = slugify(name, default="edition")
        candidate = base
        index = 2
        while (self.paths.projects / candidate).exists():
            candidate = f"{base}_{index}"
            index += 1
        return candidate

    def _write_manifest(self, handle: ProjectHandle, spec: ProjectSpec | None = None) -> Path:
        """Write ``project.json`` from the current database state."""
        project = handle.project()
        payload = {
            "manifest_version": MANIFEST_VERSION,
            "slug": handle.slug,
            "name": project["name"],
            "publication_name": project["publication_name"],
            "edition_date": project["edition_date"],
            "language": project["language"],
            "product_type": project["product_type"],
            "page_size": project["page_size"],
            "page_width_mm": project["page_width_mm"],
            "page_height_mm": project["page_height_mm"],
            "page_count": project["page_count"],
            "template_id": project["template_id"],
            "design_style": project["design_style"],
            "status": project["status"],
            "created_at": project["created_at"],
            "updated_at": datetime.now(UTC).isoformat(),
            "application": "AI Newspaper Studio",
        }
        if spec is not None:
            payload["ai_provider"] = spec.ai_provider
        return write_json(handle.manifest_path, payload)

    # --------------------------------------------------------------- open
    def open(self, slug_or_path: str | Path) -> ProjectHandle:
        """Open an existing project by slug or directory."""
        candidate = Path(slug_or_path)
        directory = (
            candidate
            if candidate.is_absolute() or candidate.exists()
            else self.paths.projects / str(slug_or_path)
        )
        directory = directory.resolve()
        if not directory.exists():
            raise ProjectError(
                f"Project not found: {slug_or_path}",
                recovery_action="Pick a project from the Projects page.",
            )
        if not (directory / "database.sqlite").exists():
            raise ProjectError(
                f"{directory} does not contain a project database",
                recovery_action="Open the folder that contains project.json.",
            )
        slug = directory.name
        if slug in self._open:
            return self._open[slug]

        for sub in SUBDIRECTORIES:
            (directory / sub).mkdir(parents=True, exist_ok=True)
        database = ProjectDatabase(directory)
        with database.scope() as session:
            row = database.project(session)
            row.directory = str(directory)
            project_id = row.id
            self.app_db.register_project(row)

        handle = ProjectHandle(
            slug=slug,
            directory=directory,
            database=database,
            versions=VersionManager(directory),
            project_id=project_id,
        )
        self._open[slug] = handle
        log.info("Opened project '%s'", slug)
        self._emit(EventType.PROJECT_OPENED, slug=slug, directory=str(directory))
        return handle

    def close(self, slug: str) -> None:
        """Close an open project."""
        handle = self._open.pop(slug, None)
        if handle is not None:
            handle.close()
            log.info("Closed project '%s'", slug)

    def close_all(self) -> None:
        """Close every open project."""
        for slug in list(self._open):
            self.close(slug)

    shutdown = close_all

    # --------------------------------------------------------------- list
    def list_projects(self) -> list[dict[str, Any]]:
        """Every project known to the registry, plus any found on disk."""
        registered = {row["slug"]: row for row in self.app_db.list_projects()}
        if self.paths.projects.exists():
            for directory in sorted(self.paths.projects.iterdir()):
                if not directory.is_dir() or directory.name in registered:
                    continue
                manifest = read_json(directory / PROJECT_MANIFEST, None)
                if manifest:
                    manifest["directory"] = str(directory)
                    manifest["slug"] = manifest.get("slug", directory.name)
                    registered[manifest["slug"]] = manifest
        rows = list(registered.values())
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return rows

    def exists(self, slug: str) -> bool:
        """Whether a project directory exists."""
        return (self.paths.projects / slug).exists()

    # ------------------------------------------------------------- update
    def update(self, handle: ProjectHandle, **fields: Any) -> dict[str, Any]:
        """Update project fields and refresh the manifest."""
        allowed = {
            "name",
            "publication_name",
            "edition_date",
            "language",
            "product_type",
            "page_size",
            "page_width_mm",
            "page_height_mm",
            "page_count",
            "template_id",
            "design_style",
            "status",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ProjectError(f"Unknown project field(s): {', '.join(sorted(unknown))}")
        with handle.database.scope() as session:
            row = session.get(E.Project, handle.project_id)
            if row is None:
                raise DatabaseError("Project row missing")
            for key, value in fields.items():
                setattr(row, key, value)
            self.app_db.register_project(row)
        self._write_manifest(handle)
        self._emit(EventType.PROJECT_CHANGED, slug=handle.slug, fields=sorted(fields))
        return handle.project()

    def save(self, handle: ProjectHandle) -> Path:
        """Flush the manifest to disk."""
        path = self._write_manifest(handle)
        self._emit(EventType.PROJECT_SAVED, slug=handle.slug)
        return path

    # ------------------------------------------------------------ recovery
    def resumable(self, handle: ProjectHandle) -> dict[str, Any] | None:
        """Details of an interrupted run, or ``None`` (specification §30)."""
        with handle.uow() as uow:
            run = uow.runs.resumable(handle.project_id)
            if run is None:
                return None
            return {
                "run_id": run.id,
                "stage": run.stage,
                "completed_stages": run.stages_done,
                "iteration": run.iteration,
                "score": run.score,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "mode": run.mode,
            }

    def mark_interrupted_runs(self, handle: ProjectHandle) -> int:
        """Flag runs left ``running`` by a crash so the UI can offer a resume."""
        count = 0
        with handle.uow() as uow:
            for run in uow.runs.all():
                if run.status == "running":
                    run.status = "interrupted"
                    count += 1
        if count:
            log.warning("Marked %d interrupted run(s) in project '%s'", count, handle.slug)
        return count

    # ------------------------------------------------------------- archive
    def archive(self, handle: ProjectHandle, target: Path | None = None) -> Path:
        """Zip the whole project for hand-off or backup."""
        destination = Path(target) if target else handle.output_dir / "archive" / f"{handle.slug}.zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive = make_archive(handle.directory, destination, exclude={"versions", "__pycache__"})
        log.info("Archived project '%s' -> %s", handle.slug, archive)
        return archive

    def duplicate(self, handle: ProjectHandle, new_name: str) -> ProjectHandle:
        """Copy a project under a new name (content and assets included)."""
        slug = self._unique_slug(new_name)
        target = self.paths.projects / slug
        shutil.copytree(handle.directory, target, ignore=shutil.ignore_patterns("versions", "output"))
        database = ProjectDatabase(target)
        with database.scope() as session:
            row = database.project(session)
            row.slug = slug
            row.name = new_name
            row.directory = str(target)
            row.status = "draft"
            project_id = row.id
            self.app_db.register_project(row)
        new_handle = ProjectHandle(
            slug=slug,
            directory=target,
            database=database,
            versions=VersionManager(target),
            project_id=project_id,
        )
        self._write_manifest(new_handle)
        self._open[slug] = new_handle
        return new_handle

    def delete(self, slug: str, *, remove_files: bool = False) -> bool:
        """Unregister a project and, optionally, delete its files."""
        self.close(slug)
        self.app_db.unregister_project(slug)
        directory = self.paths.projects / slug
        if remove_files and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            log.warning("Deleted project directory %s", directory)
            return True
        return directory.exists()

    def statistics(self, handle: ProjectHandle) -> dict[str, Any]:
        """Counts shown on the Dashboard."""
        with handle.uow() as uow:
            articles = uow.articles.for_project(handle.project_id)
            assets = uow.assets.for_project(handle.project_id)
            pages = uow.pages.for_project(handle.project_id)
            run = uow.runs.latest(handle.project_id)
            return {
                "articles": len(articles),
                "words": sum(a.word_count for a in articles),
                "assets": len(assets),
                "generated_assets": sum(1 for a in assets if a.ai_generated),
                "pages": len(pages),
                "built_pages": sum(1 for p in pages if p.status == "built"),
                "empty_pages": sum(1 for p in pages if p.status == "empty"),
                "average_score": _mean([p.qa_score for p in pages if p.status != "empty"]),
                "last_run": {"status": run.status, "stage": run.stage, "score": run.score} if run else None,
            }

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)


def _mean(values: list[float]) -> float:
    """Mean of *values*, or ``0.0`` when there are none."""
    return round(sum(values) / len(values), 2) if values else 0.0


def default_edition_name(publication: str, when: date | None = None) -> str:
    """Suggest a project name such as ``"Morning Post - 2026-08-20"``."""
    when = when or date.today()
    return f"{publication.strip() or 'Edition'} - {when.isoformat()}"
