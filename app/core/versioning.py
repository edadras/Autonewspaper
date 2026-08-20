"""Project version snapshots.

Before any destructive or large-scale operation the project state is
snapshotted into ``versions/version_NNN``. A snapshot contains the project
manifest, the SQLite database and the current layout plan - enough to restore
the editorial and layout state without duplicating the (large) asset files,
which are content-addressed and never mutated in place.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION_RE = re.compile(r"^version_(\d{3,})$")
SNAPSHOT_FILES = ("project.json", "database.sqlite", "layout_plan.json", "editorial_plan.json")


@dataclass
class VersionInfo:
    """Metadata describing one snapshot."""

    number: int
    name: str
    path: Path
    created_at: datetime
    label: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation."""
        return {
            "number": self.number,
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at.isoformat(),
            "label": self.label,
            "note": self.note,
        }


class VersionManager:
    """Creates, lists and restores project snapshots."""

    def __init__(self, project_dir: Path, keep: int = 50) -> None:
        self.project_dir = Path(project_dir)
        self.versions_dir = self.project_dir / "versions"
        self.keep = max(1, keep)

    def _next_number(self) -> int:
        return max((v.number for v in self.list()), default=0) + 1

    def list(self) -> list[VersionInfo]:
        """Every snapshot, oldest first."""
        if not self.versions_dir.exists():
            return []
        out: list[VersionInfo] = []
        for entry in sorted(self.versions_dir.iterdir()):
            match = VERSION_RE.match(entry.name)
            if not entry.is_dir() or not match:
                continue
            meta_file = entry / "version.json"
            meta: dict[str, Any] = {}
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:  # pragma: no cover
                    meta = {}
            created = meta.get("created_at")
            out.append(
                VersionInfo(
                    number=int(match.group(1)),
                    name=entry.name,
                    path=entry,
                    created_at=(
                        datetime.fromisoformat(created)
                        if created
                        else datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
                    ),
                    label=meta.get("label", entry.name),
                    note=meta.get("note", ""),
                )
            )
        return out

    def create(self, label: str, note: str = "", extra: dict[str, Any] | None = None) -> VersionInfo:
        """Snapshot the current project state and return its descriptor."""
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        number = self._next_number()
        target = self.versions_dir / f"version_{number:03d}"
        target.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        for filename in SNAPSHOT_FILES:
            source = self.project_dir / filename
            if source.exists():
                shutil.copy2(source, target / filename)
                copied.append(filename)

        adobe_dir = self.project_dir / "adobe"
        if adobe_dir.exists():
            for script in adobe_dir.rglob("*.jsx"):
                rel = script.relative_to(self.project_dir)
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(script, dest)
                copied.append(str(rel))

        info = VersionInfo(
            number=number,
            name=target.name,
            path=target,
            created_at=datetime.now(UTC),
            label=label,
            note=note,
        )
        payload = info.to_dict()
        payload["files"] = copied
        payload["extra"] = extra or {}
        (target / "version.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Created %s (%s) with %d file(s)", target.name, label, len(copied))
        self._prune()
        return info

    def restore(self, number: int) -> VersionInfo:
        """Restore snapshot *number*, snapshotting the current state first."""
        versions = {v.number: v for v in self.list()}
        if number not in versions:
            raise FileNotFoundError(f"version_{number:03d} does not exist")
        target = versions[number]
        self.create(label=f"Auto-backup before restoring {target.name}", note="automatic")
        for item in target.path.rglob("*"):
            if item.is_dir() or item.name == "version.json":
                continue
            rel = item.relative_to(target.path)
            dest = self.project_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
        log.info("Restored %s", target.name)
        return target

    def delete(self, number: int) -> bool:
        """Delete a snapshot; returns ``True`` when something was removed."""
        for version in self.list():
            if version.number == number:
                shutil.rmtree(version.path, ignore_errors=True)
                return True
        return False

    def _prune(self) -> None:
        versions = self.list()
        for version in versions[: max(0, len(versions) - self.keep)]:
            shutil.rmtree(version.path, ignore_errors=True)
