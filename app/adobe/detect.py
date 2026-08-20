"""Adobe application discovery.

Installation paths are never hard-coded (specification §17). Detection walks,
in order:

1. an explicit path configured by the operator,
2. the ``AINS_INDESIGN_PATH`` / ``AINS_PHOTOSHOP_PATH`` environment variables,
3. the Windows registry (Adobe's own keys and the uninstall entries),
4. the registered COM ProgIDs,
5. the conventional ``Program Files`` locations,
6. ``PATH``.

Everything degrades gracefully on non-Windows machines, where the module
reports "not installed" instead of raising - the rest of the application then
runs in planning-only mode.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

INDESIGN_EXE = "InDesign.exe"
PHOTOSHOP_EXE = "Photoshop.exe"

# ProgIDs Adobe registers; the un-versioned one points at the newest install.
INDESIGN_PROGIDS = ["InDesign.Application"] + [
    f"InDesign.Application.{year}" for year in range(2026, 2013, -1)
]
PHOTOSHOP_PROGIDS = ["Photoshop.Application"] + [
    f"Photoshop.Application.{version}" for version in range(180, 120, -5)
]

_VERSION_RE = re.compile(r"(20\d{2}|CC\s?20\d{2}|\d+\.\d+)")


@dataclass
class AdobeApp:
    """A detected (or missing) Adobe application."""

    kind: str
    """``"indesign"`` or ``"photoshop"``."""
    installed: bool = False
    executable: Path | None = None
    version: str = ""
    prog_id: str = ""
    scripts_dir: Path | None = None
    detection_method: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form for diagnostics and settings."""
        return {
            "kind": self.kind,
            "installed": self.installed,
            "executable": str(self.executable) if self.executable else None,
            "version": self.version,
            "prog_id": self.prog_id,
            "scripts_dir": str(self.scripts_dir) if self.scripts_dir else None,
            "detection_method": self.detection_method,
            "notes": self.notes,
        }

    def summary(self) -> str:
        """One-line description for the log."""
        if not self.installed:
            return f"{self.kind}: not detected"
        return (
            f"{self.kind}: {self.version or 'unknown version'} at {self.executable} "
            f"(via {self.detection_method})"
        )


def _read_registry_value(root: Any, subkey: str, name: str) -> str | None:
    """Read a single registry value, returning ``None`` when absent."""
    if not IS_WINDOWS:
        return None
    import winreg  # type: ignore

    for access in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
        try:
            with winreg.OpenKey(root, subkey, 0, access) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value)
        except OSError:
            continue
    return None


def _enumerate_subkeys(root: Any, subkey: str) -> list[str]:
    """List the direct child keys of a registry key."""
    if not IS_WINDOWS:
        return []
    import winreg  # type: ignore

    out: list[str] = []
    try:
        with winreg.OpenKey(root, subkey) as key:
            index = 0
            while True:
                try:
                    out.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except OSError:
        return []
    return out


def _from_adobe_registry(product: str, exe_name: str) -> tuple[Path | None, str]:
    """Look the product up under ``HKLM\\SOFTWARE\\Adobe``."""
    if not IS_WINDOWS:
        return (None, "")
    import winreg  # type: ignore

    base = f"SOFTWARE\\Adobe\\{product}"
    versions = sorted(_enumerate_subkeys(winreg.HKEY_LOCAL_MACHINE, base), reverse=True)
    for version in versions:
        for value_name in ("InstallPath", "ApplicationPath", "Path"):
            for suffix in ("", "\\Installer", "\\Core"):
                path = _read_registry_value(
                    winreg.HKEY_LOCAL_MACHINE, f"{base}\\{version}{suffix}", value_name
                )
                if not path:
                    continue
                candidate = Path(path)
                if candidate.is_file() and candidate.name.lower() == exe_name.lower():
                    return (candidate, version)
                executable = candidate / exe_name
                if executable.exists():
                    return (executable, version)
        for subkey in _enumerate_subkeys(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{version}"):
            path = _read_registry_value(
                winreg.HKEY_LOCAL_MACHINE, f"{base}\\{version}\\{subkey}", "ApplicationPath"
            )
            if path and (Path(path) / exe_name).exists():
                return (Path(path) / exe_name, version)
    return (None, "")


def _from_uninstall_registry(display_name: str, exe_name: str) -> tuple[Path | None, str]:
    """Scan the Windows uninstall entries for the product."""
    if not IS_WINDOWS:
        return (None, "")
    import winreg  # type: ignore

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
    ]
    best: tuple[Path | None, str] = (None, "")
    for root, base in roots:
        for entry in _enumerate_subkeys(root, base):
            name = _read_registry_value(root, f"{base}\\{entry}", "DisplayName") or ""
            if display_name.lower() not in name.lower():
                continue
            location = _read_registry_value(root, f"{base}\\{entry}", "InstallLocation") or ""
            version_match = _VERSION_RE.search(name)
            version = (
                version_match.group(1)
                if version_match
                else (_read_registry_value(root, f"{base}\\{entry}", "DisplayVersion") or "")
            )
            if location:
                executable = Path(location) / exe_name
                if executable.exists() and (best[0] is None or version > best[1]):
                    best = (executable, version)
    return best


def _from_prog_ids(prog_ids: Iterable[str]) -> tuple[str, str]:
    """Find a registered COM ProgID and its CLSID-derived executable path."""
    if not IS_WINDOWS:
        return ("", "")
    import winreg  # type: ignore

    for prog_id in prog_ids:
        clsid = _read_registry_value(winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\CLSID", "")
        if clsid:
            server = _read_registry_value(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\LocalServer32", "")
            return (prog_id, (server or "").strip('"').split('" ')[0])
    return ("", "")


def _known_locations(folder_prefix: str, exe_name: str) -> list[Path]:
    """Conventional installation directories, newest first."""
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(r"C:\Program Files"),
        Path("/Applications"),
    ]
    candidates: list[Path] = []
    for root in roots:
        adobe = root / "Adobe"
        if not adobe.exists():
            continue
        for child in sorted(adobe.iterdir(), reverse=True):
            if child.is_dir() and child.name.lower().startswith(folder_prefix.lower()):
                executable = child / exe_name
                if executable.exists():
                    candidates.append(executable)
    return candidates


def _scripts_dir(kind: str, version: str) -> Path | None:
    """User "Scripts Panel" folder, used by the file-based automation path."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        product = "InDesign" if kind == "indesign" else "Adobe Photoshop"
        base = Path(appdata) / "Adobe" / product
        if not base.exists():
            return None
        versions = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
        for candidate in versions:
            if version and version not in candidate.name:
                continue
            scripts = (
                candidate / "en_US" / "Scripts" / "Scripts Panel"
                if kind == "indesign"
                else candidate / "Presets" / "Scripts"
            )
            if scripts.exists():
                return scripts
            scripts.mkdir(parents=True, exist_ok=True)
            return scripts
        return None
    if sys.platform == "darwin":  # pragma: no cover - macOS convenience
        product = "InDesign" if kind == "indesign" else "Adobe Photoshop"
        base = Path.home() / "Library" / "Preferences" / f"Adobe {product}"
        return base if base.exists() else None
    return None


def _version_from_path(path: Path) -> str:
    """Derive a version string from the installation folder name."""
    match = _VERSION_RE.search(path.parent.name)
    return match.group(1) if match else ""


def detect_app(
    kind: str,
    *,
    configured_path: str | None = None,
) -> AdobeApp:
    """Detect InDesign or Photoshop, honouring an explicitly configured path."""
    exe_name = INDESIGN_EXE if kind == "indesign" else PHOTOSHOP_EXE
    display = "Adobe InDesign" if kind == "indesign" else "Adobe Photoshop"
    registry_product = "InDesign" if kind == "indesign" else "Photoshop"
    folder_prefix = "Adobe InDesign" if kind == "indesign" else "Adobe Photoshop"
    prog_ids = INDESIGN_PROGIDS if kind == "indesign" else PHOTOSHOP_PROGIDS

    app = AdobeApp(kind=kind)

    # 1. Explicit configuration ------------------------------------------
    if configured_path:
        candidate = Path(configured_path)
        if candidate.is_dir():
            candidate = candidate / exe_name
        if candidate.exists():
            app.installed = True
            app.executable = candidate
            app.version = _version_from_path(candidate)
            app.detection_method = "configured"
        else:
            app.notes.append(f"Configured path does not exist: {configured_path}")

    # 2. Environment ------------------------------------------------------
    if not app.installed:
        env_value = os.environ.get(f"AINS_{kind.upper()}_PATH")
        if env_value and Path(env_value).exists():
            app.installed = True
            app.executable = Path(env_value)
            app.version = _version_from_path(Path(env_value))
            app.detection_method = "environment"

    # 3. Adobe registry keys ---------------------------------------------
    if not app.installed:
        executable, version = _from_adobe_registry(registry_product, exe_name)
        if executable:
            app.installed = True
            app.executable = executable
            app.version = version or _version_from_path(executable)
            app.detection_method = "registry:adobe"

    # 4. Uninstall entries ------------------------------------------------
    if not app.installed:
        executable, version = _from_uninstall_registry(display, exe_name)
        if executable:
            app.installed = True
            app.executable = executable
            app.version = version or _version_from_path(executable)
            app.detection_method = "registry:uninstall"

    # 5. Conventional locations -------------------------------------------
    if not app.installed:
        for candidate in _known_locations(folder_prefix, exe_name):
            app.installed = True
            app.executable = candidate
            app.version = _version_from_path(candidate)
            app.detection_method = "known-location"
            break

    # 6. PATH --------------------------------------------------------------
    if not app.installed:
        found = shutil.which(exe_name)
        if found:
            app.installed = True
            app.executable = Path(found)
            app.version = _version_from_path(Path(found))
            app.detection_method = "path"

    # COM registration is independent of how the executable was found.
    prog_id, com_path = _from_prog_ids(prog_ids)
    if prog_id:
        app.prog_id = prog_id
        if not app.installed and com_path and Path(com_path).exists():
            app.installed = True
            app.executable = Path(com_path)
            app.version = _version_from_path(Path(com_path))
            app.detection_method = "com"
    elif IS_WINDOWS:
        app.notes.append("No COM ProgID registered; scripting will use the file-based path.")

    if app.installed:
        app.scripts_dir = _scripts_dir(kind, app.version)
        if app.scripts_dir is None:
            app.notes.append("Scripts Panel folder not found; queue automation will use a temp folder.")
    elif not IS_WINDOWS:
        app.notes.append(
            f"{display} automation requires Windows; the application runs in planning-only mode here."
        )
    else:
        app.notes.append(f"{display} was not found on this machine.")

    log.info(app.summary())
    return app


def detect_indesign(configured_path: str | None = None) -> AdobeApp:
    """Detect Adobe InDesign."""
    return detect_app("indesign", configured_path=configured_path)


def detect_photoshop(configured_path: str | None = None) -> AdobeApp:
    """Detect Adobe Photoshop."""
    return detect_app("photoshop", configured_path=configured_path)


def detect_all(indesign_path: str | None = None, photoshop_path: str | None = None) -> dict[str, AdobeApp]:
    """Detect both applications in one call."""
    return {
        "indesign": detect_indesign(indesign_path),
        "photoshop": detect_photoshop(photoshop_path),
    }
