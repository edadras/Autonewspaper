"""Filesystem layout of the application.

Every path used by the application is resolved through this module so that the
program behaves identically when it runs from source, from a virtual
environment or from the frozen (PyInstaller) Windows build.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "AINewspaperStudio"


def _is_frozen() -> bool:
    """Return ``True`` when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def package_root() -> Path:
    """Directory that contains the ``app`` package."""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """Read-only resources that ship with the application."""
    return package_root()


def user_data_dir() -> Path:
    """Writable per-user directory (``%LOCALAPPDATA%`` on Windows)."""
    override = os.environ.get("AINS_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_DIR_NAME


@dataclass(frozen=True)
class AppPaths:
    """Resolved application paths.

    Attributes are plain :class:`pathlib.Path` objects; :meth:`ensure` creates
    every writable directory so that the rest of the code never has to.
    """

    root: Path
    data: Path
    projects: Path
    templates: Path
    prompts: Path
    logs: Path
    cache: Path
    settings_file: Path
    scripts: Path
    resources: Path

    @classmethod
    def resolve(cls, data_dir: Path | None = None) -> AppPaths:
        """Build the path set, optionally rooted at an explicit *data_dir*."""
        root = package_root()
        data = Path(data_dir).resolve() if data_dir else user_data_dir()
        return cls(
            root=root,
            data=data,
            projects=data / "projects",
            templates=data / "templates",
            prompts=root / "prompts",
            logs=data / "logs",
            cache=data / "cache",
            settings_file=data / "settings.json",
            scripts=root / "app" / "adobe" / "scripts",
            resources=root / "resources",
        )

    def ensure(self) -> AppPaths:
        """Create every writable directory. Returns ``self`` for chaining."""
        for path in (self.data, self.projects, self.templates, self.logs, self.cache):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def builtin_templates(self) -> Path:
        """Directory holding the templates shipped with the application."""
        return self.root / "templates"


_PATHS: AppPaths | None = None


def get_paths() -> AppPaths:
    """Return the process-wide :class:`AppPaths` singleton."""
    global _PATHS
    if _PATHS is None:
        _PATHS = AppPaths.resolve().ensure()
    return _PATHS


def set_paths(paths: AppPaths) -> AppPaths:
    """Override the process-wide path set (used by tests and the installer)."""
    global _PATHS
    _PATHS = paths.ensure()
    return _PATHS
