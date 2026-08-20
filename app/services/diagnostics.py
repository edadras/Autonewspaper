"""System diagnostics (specification §49).

Everything the application depends on is probed and reported in one place, so
an operator can see why a run would fail before starting it.
"""

from __future__ import annotations

import importlib
import logging
import platform
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adobe.service import AdobeService
from app.ai.registry import AIService
from app.config.paths import AppPaths
from app.config.settings import SettingsManager
from app.templates.manager import TemplateManager
from app.utils.files import free_space_bytes, human_size, is_writable
from app.vision.fonts import check_fonts, resolve_font_file
from app.vision.renderer import shaping_engine

log = logging.getLogger(__name__)

Status = str  # "ok" | "warning" | "error" | "info"


@dataclass
class Check:
    """One diagnostic line."""

    name: str
    status: Status
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether this check passed."""
        return self.status in ("ok", "info")

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"name": self.name, "status": self.status, "detail": self.detail, "data": self.data}


@dataclass
class DiagnosticsReport:
    """Full diagnostics run."""

    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether nothing errored."""
        return all(check.status != "error" for check in self.checks)

    def by_status(self, status: Status) -> list[Check]:
        """Checks with a given status."""
        return [check for check in self.checks if check.status == status]

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "ok": self.ok,
            "errors": len(self.by_status("error")),
            "warnings": len(self.by_status("warning")),
            "checks": [check.to_dict() for check in self.checks],
        }

    def render(self) -> str:
        """Plain-text report."""
        symbol = {"ok": "✓", "info": "•", "warning": "!", "error": "✗"}
        return "\n".join(
            f"{symbol.get(c.status, '?')} {c.name}: {c.detail}" for c in self.checks
        )


class DiagnosticsService:
    """Runs the System Diagnostics page's checks."""

    def __init__(
        self,
        paths: AppPaths,
        settings: SettingsManager,
        *,
        ai: AIService | None = None,
        adobe: AdobeService | None = None,
        templates: TemplateManager | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.ai = ai
        self.adobe = adobe
        self.templates = templates

    def run(self, *, deep: bool = False) -> DiagnosticsReport:
        """Run every check; *deep* also contacts Adobe and the AI provider."""
        report = DiagnosticsReport()
        report.checks.append(self._python())
        report.checks.extend(self._packages())
        report.checks.append(self._sqlite())
        report.checks.append(self._storage())
        report.checks.append(self._permissions())
        report.checks.append(self._templates())
        report.checks.append(self._fonts())
        report.checks.append(self._shaping())
        report.checks.append(self._internet())
        report.checks.extend(self._adobe(deep=deep))
        report.checks.extend(self._ai(deep=deep))
        report.checks.append(self._secrets())
        log.info("Diagnostics:\n%s", report.render())
        return report

    # ------------------------------------------------------------- runtime
    def _python(self) -> Check:
        version = sys.version.split()[0]
        status = "ok" if sys.version_info >= (3, 11) else "error"
        return Check(
            "Python",
            status,
            f"{version} on {platform.system()} {platform.release()} ({platform.machine()})",
            {"version": version, "executable": sys.executable, "frozen": getattr(sys, "frozen", False)},
        )

    def _packages(self) -> list[Check]:
        required = {
            "PySide6": "Desktop UI",
            "sqlalchemy": "Database",
            "pydantic": "Configuration and data models",
            "PIL": "Image processing",
            "httpx": "AI provider transport",
        }
        optional = {
            "docx": "Word import",
            "pypdf": "PDF import",
            "bs4": "HTML import",
            "arabic_reshaper": "Persian shaping fallback",
            "win32com": "Adobe COM automation (Windows)",
            "pywinauto": "UI automation fallback (Windows)",
            "cv2": "Faster template matching and face detection",
        }
        checks: list[Check] = []
        for module, purpose in required.items():
            checks.append(self._module_check(module, purpose, required=True))
        missing = []
        for module, purpose in optional.items():
            check = self._module_check(module, purpose, required=False)
            if not check.ok:
                missing.append(f"{module} ({purpose})")
        checks.append(
            Check(
                "Optional packages",
                "ok" if not missing else "warning",
                "all present" if not missing else "not installed: " + ", ".join(missing),
                {"missing": missing},
            )
        )
        return checks

    @staticmethod
    def _module_check(module: str, purpose: str, *, required: bool) -> Check:
        try:
            imported = importlib.import_module(module)
            version = getattr(imported, "__version__", "") or ""
            return Check(f"Package {module}", "ok", f"{version} - {purpose}".strip(" -"))
        except Exception as exc:  # noqa: BLE001
            return Check(
                f"Package {module}",
                "error" if required else "warning",
                f"not available ({exc}) - {purpose}",
            )

    def _sqlite(self) -> Check:
        try:
            connection = sqlite3.connect(":memory:")
            connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO probe VALUES (1)")
            connection.commit()
            connection.close()
            return Check("SQLite", "ok", f"version {sqlite3.sqlite_version}")
        except Exception as exc:  # noqa: BLE001
            return Check("SQLite", "error", str(exc)[:200])

    # ------------------------------------------------------------- storage
    def _storage(self) -> Check:
        free = free_space_bytes(self.paths.data)
        status = "ok" if free > 2 * 1024**3 else ("warning" if free > 512 * 1024**2 else "error")
        return Check(
            "Storage",
            status,
            f"{human_size(free)} free at {self.paths.data}",
            {"free_bytes": free, "data_dir": str(self.paths.data)},
        )

    def _permissions(self) -> Check:
        targets = {
            "data": self.paths.data,
            "projects": self.paths.projects,
            "logs": self.paths.logs,
            "cache": self.paths.cache,
            "output": self.settings.output_dir(),
        }
        failed = [name for name, path in targets.items() if not is_writable(path)]
        return Check(
            "Permissions",
            "ok" if not failed else "error",
            "all directories writable" if not failed else f"not writable: {', '.join(failed)}",
            {name: str(path) for name, path in targets.items()},
        )

    # ----------------------------------------------------------- resources
    def _templates(self) -> Check:
        manager = self.templates or TemplateManager(self.paths)
        try:
            summaries = manager.list_summaries()
        except Exception as exc:  # noqa: BLE001
            return Check("Templates", "error", str(exc)[:200])
        if not summaries:
            return Check("Templates", "error", "no templates found")
        default = self.settings.settings.default_template_id
        warnings = []
        if default not in {s["id"] for s in summaries}:
            warnings.append(f"the default template '{default}' is missing")
        else:
            warnings.extend(manager.validate(default))
        return Check(
            "Templates",
            "ok" if not warnings else "warning",
            f"{len(summaries)} template(s)" + ("; " + "; ".join(warnings) if warnings else ""),
            {"templates": [s["id"] for s in summaries]},
        )

    def _fonts(self) -> Check:
        families = self.settings.settings.default_fonts
        script = "arabic" if self.settings.settings.ui.language in ("fa", "ar") else "latin"
        found = check_fonts(families, script)
        missing = [key.split(":", 1)[1] for key, value in found.items() if value is None]
        substituted = [
            key.split(":", 1)[1]
            for key, value in found.items()
            if value and Path(value).stem.lower().replace(" ", "")
            not in key.split(":", 1)[1].lower().replace(" ", "")
        ]
        if missing:
            status, detail = "error", f"no usable font for: {', '.join(missing)}"
        elif substituted:
            status = "warning"
            detail = f"substituted: {', '.join(sorted(set(substituted)))} (previews only; InDesign uses its own fonts)"
        else:
            status, detail = "ok", "all configured fonts resolved"
        return Check("Fonts", status, detail, {"resolved": found})

    def _shaping(self) -> Check:
        engine = shaping_engine()
        if engine == "none":
            return Check(
                "Right-to-left shaping",
                "warning",
                "no shaping engine; Persian previews will be unshaped "
                "(install a Raqm-enabled Pillow or arabic-reshaper + python-bidi)",
            )
        return Check("Right-to-left shaping", "ok", f"using {engine}")

    def _internet(self) -> Check:
        provider = self.settings.settings.ai.provider
        if provider in ("heuristic", "local"):
            return Check(
                "Internet", "info", f"not required by the '{provider}' provider"
            )
        try:
            socket.setdefaulttimeout(4.0)
            with socket.create_connection(("1.1.1.1", 443), timeout=4.0):
                pass
            return Check("Internet", "ok", "outbound HTTPS reachable")
        except OSError as exc:
            return Check(
                "Internet",
                "warning",
                f"no outbound connection ({exc}); the offline analyser will be used",
            )

    # -------------------------------------------------------------- adobe
    def _adobe(self, *, deep: bool) -> list[Check]:
        service = self.adobe
        if service is None:
            service = AdobeService(self.settings)
        checks: list[Check] = []
        for name, app in (("InDesign", service.indesign_app), ("Photoshop", service.photoshop_app)):
            if app.installed:
                checks.append(
                    Check(
                        f"Adobe {name}",
                        "ok",
                        f"{app.version or 'version unknown'} at {app.executable}",
                        app.to_dict(),
                    )
                )
            else:
                checks.append(
                    Check(
                        f"Adobe {name}",
                        "warning",
                        "not detected; "
                        + (
                            "the layout plan, previews and PDF will come from the built-in renderer"
                            if name == "InDesign"
                            else "image processing will use the built-in engine"
                        ),
                        app.to_dict(),
                    )
                )
        if deep and service.indesign_app.installed:
            info = service.indesign.health()
            checks.append(
                Check(
                    "InDesign scripting",
                    "ok" if info.get("version") else "error",
                    str(info.get("version") or info.get("error", "unreachable"))[:200],
                    info,
                )
            )
        return checks

    # ----------------------------------------------------------------- ai
    def _ai(self, *, deep: bool) -> list[Check]:
        config = self.settings.settings.ai
        checks = [
            Check(
                "AI configuration",
                "ok",
                f"text={config.provider}/{config.model}, vision={config.vision_provider}/{config.vision_model}, "
                f"images={self.settings.settings.image_ai.provider}",
                {"provider": config.provider, "model": config.model},
            )
        ]
        if not deep or self.ai is None:
            return checks
        for health in self.ai.health():
            checks.append(
                Check(
                    f"AI {health.name}",
                    "ok" if health.available else "warning",
                    health.detail[:200],
                    health.to_dict(),
                )
            )
        return checks

    def _secrets(self) -> Check:
        store = self.settings.secrets
        providers = ["openai", "anthropic", "gemini", "stablediffusion"]
        present = [name for name in providers if store.has(f"{name}_api_key")]
        return Check(
            "Secret storage",
            "ok",
            f"backend={store.backend_name()}; keys stored for: "
            + (", ".join(present) if present else "none"),
            {"backend": store.backend_name(), "providers": present},
        )


def quick_report(paths: AppPaths, settings: SettingsManager) -> dict[str, Any]:
    """Shallow diagnostics used at start-up."""
    return DiagnosticsService(paths, settings).run(deep=False).to_dict()
