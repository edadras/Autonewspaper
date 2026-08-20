"""Template discovery, validation, import and export."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from app.config.paths import AppPaths
from app.core.errors import TemplateError
from app.database.session import AppDatabase
from app.models import entities as E  # noqa: N812
from app.models.entities import JSONMixin
from app.templates.schema import TemplateSpec
from app.utils.files import safe_filename, slugify, unique_path

log = logging.getLogger(__name__)

SUFFIX = ".template.json"


class TemplateManager:
    """Owns the template catalogue.

    Built-in templates ship with the application and are read-only; user
    templates live in the writable data directory and can be created,
    duplicated, edited, imported and exported.
    """

    def __init__(self, paths: AppPaths, app_db: AppDatabase | None = None) -> None:
        self.paths = paths
        self.app_db = app_db
        self._cache: dict[str, TemplateSpec] = {}
        self._sources: dict[str, Path] = {}
        self._builtin_ids: set[str] = set()

    # ------------------------------------------------------------ discovery
    def discover(self, force: bool = False) -> list[TemplateSpec]:
        """Scan both template directories and return every valid template."""
        if self._cache and not force:
            return list(self._cache.values())
        self._cache.clear()
        self._sources.clear()
        self._builtin_ids.clear()

        for directory, builtin in (
            (self.paths.builtin_templates(), True),
            (self.paths.templates, False),
        ):
            if not directory.exists():
                continue
            for file in sorted(directory.glob(f"*{SUFFIX}")):
                try:
                    spec = TemplateSpec.load(file)
                except Exception as exc:  # noqa: BLE001
                    log.error("Invalid template %s: %s", file.name, exc)
                    continue
                self._cache[spec.id] = spec
                self._sources[spec.id] = file
                if builtin:
                    self._builtin_ids.add(spec.id)
        log.info("Discovered %d template(s) (%d built-in)", len(self._cache), len(self._builtin_ids))
        if self.app_db is not None:
            self._sync_database()
        return list(self._cache.values())

    def _sync_database(self) -> None:
        """Mirror the catalogue into the registry database."""
        from app.database.repositories import TemplateRepository

        try:
            with self.app_db.scope() as session:  # type: ignore[union-attr]
                repo = TemplateRepository(session)
                for spec in self._cache.values():
                    repo.upsert(
                        E.TemplateRecord(
                            template_id=spec.id,
                            name=spec.name,
                            product_type=spec.product_type,
                            language=spec.language,
                            builtin=spec.id in self._builtin_ids,
                            path=str(self._sources.get(spec.id, "")),
                            data_json=JSONMixin.dump(spec.model_dump(mode="json")),
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not mirror templates into the registry: %s", exc)

    # -------------------------------------------------------------- access
    def get(self, template_id: str) -> TemplateSpec:
        """Return a template by id."""
        self.discover()
        spec = self._cache.get(template_id)
        if spec is None:
            raise TemplateError(
                f"Template '{template_id}' not found",
                context={"available": sorted(self._cache)},
                recovery_action="Choose another template in Project Settings.",
            )
        return spec

    def get_or_default(self, template_id: str) -> TemplateSpec:
        """Return a template, falling back to the first available one."""
        try:
            return self.get(template_id)
        except TemplateError:
            self.discover()
            if not self._cache:
                raise
            fallback = self._cache.get("broadsheet_fa_standard") or next(iter(self._cache.values()))
            log.warning("Template '%s' missing; using '%s'", template_id, fallback.id)
            return fallback

    def ids(self) -> list[str]:
        """Every known template id."""
        self.discover()
        return sorted(self._cache)

    def list_summaries(self, product_type: str | None = None) -> list[dict[str, Any]]:
        """Summaries for the Templates page, optionally filtered."""
        self.discover()
        out = []
        for spec in self._cache.values():
            if product_type and spec.product_type != product_type:
                continue
            summary = spec.summary()
            summary["builtin"] = spec.id in self._builtin_ids
            summary["path"] = str(self._sources.get(spec.id, ""))
            out.append(summary)
        return sorted(out, key=lambda item: (not item["builtin"], item["name"]))

    def is_builtin(self, template_id: str) -> bool:
        """Whether the template ships with the application."""
        self.discover()
        return template_id in self._builtin_ids

    def path_of(self, template_id: str) -> Path | None:
        """File the template was loaded from."""
        self.discover()
        return self._sources.get(template_id)

    # ------------------------------------------------------------ mutation
    def save(self, spec: TemplateSpec) -> Path:
        """Persist a user template (built-ins cannot be overwritten)."""
        self.discover()
        if spec.id in self._builtin_ids:
            raise TemplateError(
                f"'{spec.id}' is a built-in template and cannot be overwritten",
                recovery_action="Duplicate it first, then edit the copy.",
            )
        self.paths.templates.mkdir(parents=True, exist_ok=True)
        target = self.paths.templates / f"{safe_filename(spec.id)}{SUFFIX}"
        spec.save(target)
        self._cache[spec.id] = spec
        self._sources[spec.id] = target
        if self.app_db is not None:
            self._sync_database()
        log.info("Saved template '%s' -> %s", spec.id, target)
        return target

    def duplicate(self, template_id: str, new_id: str, new_name: str = "") -> TemplateSpec:
        """Copy a template under a new id, producing an editable user template."""
        source = self.get(template_id)
        data = source.model_dump()
        data["id"] = slugify(new_id, default="template")
        data["name"] = new_name or f"{source.name} (copy)"
        spec = TemplateSpec.model_validate(data)
        self.save(spec)
        return spec

    def delete(self, template_id: str) -> bool:
        """Delete a user template; returns ``True`` when a file was removed."""
        self.discover()
        if template_id in self._builtin_ids:
            raise TemplateError(f"'{template_id}' is built-in and cannot be deleted")
        path = self._sources.get(template_id)
        if path and path.exists():
            path.unlink()
            self._cache.pop(template_id, None)
            self._sources.pop(template_id, None)
            if self.app_db is not None:
                try:
                    with self.app_db.scope() as session:
                        row = session.query(E.TemplateRecord).filter_by(template_id=template_id).first()
                        if row is not None:
                            session.delete(row)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not unregister template: %s", exc)
            log.info("Deleted template '%s'", template_id)
            return True
        return False

    # ----------------------------------------------------- import / export
    def import_file(self, source: Path | str, *, overwrite: bool = False) -> TemplateSpec:
        """Import a ``*.template.json`` file into the user catalogue."""
        file = Path(source)
        if not file.exists():
            raise TemplateError(f"Template file not found: {file}")
        try:
            spec = TemplateSpec.load(file)
        except Exception as exc:  # noqa: BLE001
            raise TemplateError(f"'{file.name}' is not a valid template: {exc}", cause=exc) from exc
        self.discover()
        if spec.id in self._cache and not overwrite:
            spec = TemplateSpec.model_validate({**spec.model_dump(), "id": f"{spec.id}_imported"})
        self.save(spec)
        return spec

    def export_file(self, template_id: str, target: Path | str) -> Path:
        """Write a template to *target* for sharing."""
        spec = self.get(template_id)
        destination = Path(target)
        if destination.is_dir():
            destination = destination / f"{spec.id}{SUFFIX}"
        destination = unique_path(destination)
        spec.save(destination)
        log.info("Exported template '%s' -> %s", template_id, destination)
        return destination

    def install_builtin(self) -> int:
        """Copy any missing built-in template into the user directory.

        Called on first start so an operator can see - and copy - the shipped
        designs even when the installation directory is read-only.
        """
        self.paths.templates.mkdir(parents=True, exist_ok=True)
        installed = 0
        for file in self.paths.builtin_templates().glob(f"*{SUFFIX}"):
            target = self.paths.templates / f"builtin_{file.name}"
            if not target.exists():
                shutil.copy2(file, target)
                installed += 1
        if installed:
            self.discover(force=True)
        return installed

    def validate(self, template_id: str) -> list[str]:
        """Return human-readable warnings about a template's design system."""
        spec = self.get(template_id)
        warnings: list[str] = []
        required = {"headline", "body", "caption", "lead"}
        present = {style.id for style in spec.paragraph_styles}
        for missing in sorted(required - present):
            warnings.append(f"Missing paragraph style '{missing}'")
        if spec.column_width_mm() < 25:
            warnings.append(f"Column width is only {spec.column_width_mm():.1f} mm; body text may not fit")
        body = spec.paragraph_style("body")
        if body and body.size_pt < spec.layout_rules.min_body_size_pt:
            warnings.append("Body style is smaller than the template's own minimum body size")
        if not spec.master_pages:
            warnings.append("Template defines no master pages; folios will be missing")
        if not spec.pdf_presets:
            warnings.append("Template defines no PDF presets; the global defaults will be used")
        if spec.bleed_mm == 0 and spec.product_type in ("newspaper", "magazine"):
            warnings.append("Bleed is 0 mm; images cannot run off the trimmed edge")
        return warnings
