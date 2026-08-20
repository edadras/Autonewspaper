"""Adobe service facade.

Owns detection, both controllers and the pre-flight health check of
specification §18. The pipeline asks this object whether it can drive Adobe
before it starts, so a missing installation is reported up front instead of
crashing halfway through a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adobe.detect import AdobeApp, detect_indesign, detect_photoshop
from app.adobe.indesign.controller import InDesignController
from app.adobe.photoshop.controller import PhotoshopController
from app.config.settings import SettingsManager
from app.core.events import EventBus
from app.utils.files import free_space_bytes, is_writable, human_size

log = logging.getLogger(__name__)


@dataclass
class HealthCheck:
    """One line of the pre-flight report."""

    name: str
    ok: bool
    detail: str = ""
    critical: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "critical": self.critical}

    def line(self) -> str:
        """Rendered as the specification's checklist."""
        return f"{'✓' if self.ok else '✗'} {self.name}{f' - {self.detail}' if self.detail else ''}"


@dataclass
class HealthReport:
    """Result of the pre-flight check."""

    checks: list[HealthCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every critical check passed."""
        return all(check.ok for check in self.checks if check.critical)

    @property
    def can_drive_adobe(self) -> bool:
        """Whether InDesign is usable for this run."""
        return any(c.name == "InDesign installed" and c.ok for c in self.checks)

    def failures(self) -> list[HealthCheck]:
        """Checks that did not pass."""
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "ok": self.ok,
            "can_drive_adobe": self.can_drive_adobe,
            "checks": [c.to_dict() for c in self.checks],
        }

    def render(self) -> str:
        """Multi-line checklist for the log and the Diagnostics page."""
        return "\n".join(check.line() for check in self.checks)


class AdobeService:
    """Single entry point to the Adobe layer."""

    def __init__(
        self,
        settings: SettingsManager,
        work_dir: Path | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.work_dir = Path(work_dir or settings.paths.cache)
        adobe = settings.settings.adobe
        self.indesign_app: AdobeApp = detect_indesign(adobe.indesign_path)
        self.photoshop_app: AdobeApp = detect_photoshop(adobe.photoshop_path)
        self._indesign: InDesignController | None = None
        self._photoshop: PhotoshopController | None = None

    # ------------------------------------------------------------ accessors
    @property
    def indesign(self) -> InDesignController:
        """The InDesign controller (created on first use)."""
        if self._indesign is None:
            adobe = self.settings.settings.adobe
            self._indesign = InDesignController(
                self.work_dir,
                app=self.indesign_app,
                bus=self.bus,
                prefer_com=adobe.prefer_com,
                script_timeout=adobe.script_timeout_seconds,
                launch_timeout=adobe.launch_timeout_seconds,
            )
        return self._indesign

    @property
    def photoshop(self) -> PhotoshopController:
        """The Photoshop controller (created on first use)."""
        if self._photoshop is None:
            adobe = self.settings.settings.adobe
            self._photoshop = PhotoshopController(
                self.work_dir,
                app=self.photoshop_app,
                bus=self.bus,
                prefer_com=adobe.prefer_com,
                script_timeout=adobe.script_timeout_seconds,
                launch_timeout=adobe.launch_timeout_seconds,
            )
        return self._photoshop

    def redetect(self) -> dict[str, AdobeApp]:
        """Re-run detection after the operator changed the configured paths."""
        adobe = self.settings.settings.adobe
        self.indesign_app = detect_indesign(adobe.indesign_path)
        self.photoshop_app = detect_photoshop(adobe.photoshop_path)
        self._indesign = None
        self._photoshop = None
        return {"indesign": self.indesign_app, "photoshop": self.photoshop_app}

    def persist_detection(self) -> None:
        """Write the detected paths and versions back into the settings."""
        self.settings.update(
            adobe={
                "indesign_path": str(self.indesign_app.executable) if self.indesign_app.executable else None,
                "photoshop_path": str(self.photoshop_app.executable) if self.photoshop_app.executable else None,
                "indesign_version": self.indesign_app.version or None,
                "photoshop_version": self.photoshop_app.version or None,
            }
        )

    # ------------------------------------------------------------- health
    def health_check(self, output_dir: Path | None = None, *, deep: bool = False) -> HealthReport:
        """Run the pre-flight checklist of specification §18."""
        report = HealthReport()

        report.checks.append(
            HealthCheck(
                "InDesign installed",
                self.indesign_app.installed,
                self.indesign_app.summary(),
                critical=False,
            )
        )
        report.checks.append(
            HealthCheck(
                "Photoshop installed",
                self.photoshop_app.installed,
                self.photoshop_app.summary(),
            )
        )
        report.checks.append(
            HealthCheck(
                "InDesign version detected",
                bool(self.indesign_app.version),
                self.indesign_app.version or "unknown",
            )
        )
        report.checks.append(
            HealthCheck(
                "Script engine available",
                bool(self.indesign_app.prog_id) or self.indesign_app.scripts_dir is not None,
                self.indesign_app.prog_id or "file-based automation only",
            )
        )

        target = Path(output_dir or self.settings.output_dir())
        writable = is_writable(target)
        free = free_space_bytes(target if target.exists() else target.parent)
        report.checks.append(
            HealthCheck("Output directory writable", writable, str(target), critical=True)
        )
        report.checks.append(
            HealthCheck(
                "Free disk space",
                free > 512 * 1024 * 1024,
                f"{human_size(free)} available",
                critical=True,
            )
        )
        report.checks.append(
            HealthCheck(
                "Working directory writable",
                is_writable(self.work_dir),
                str(self.work_dir),
                critical=True,
            )
        )

        if deep and self.indesign_app.installed:
            info = self.indesign.health()
            report.checks.append(
                HealthCheck(
                    "InDesign reachable",
                    bool(info.get("version")) and info.get("reachable", True) is not False,
                    str(info.get("version") or info.get("error", ""))[:200],
                )
            )
            presets = info.get("pdf_presets") or []
            report.checks.append(
                HealthCheck("PDF export presets", bool(presets), f"{len(presets)} preset(s)")
            )
        if deep and self.photoshop_app.installed:
            caps = self.photoshop.capabilities()
            report.checks.append(
                HealthCheck(
                    "Photoshop reachable",
                    caps.get("reachable", True) is not False,
                    str(caps.get("version") or caps.get("error", ""))[:200],
                )
            )

        if not self.indesign_app.installed:
            report.checks.append(
                HealthCheck(
                    "Adobe automation mode",
                    True,
                    "InDesign not available - the run will produce the layout plan, previews "
                    "and a PDF from the built-in renderer.",
                )
            )

        log.info("Adobe health check:\n%s", report.render())
        return report

    def describe(self) -> dict[str, Any]:
        """Summary for the Adobe Settings page."""
        return {
            "indesign": self.indesign_app.to_dict(),
            "photoshop": self.photoshop_app.to_dict(),
            "work_dir": str(self.work_dir),
            "prefer_com": self.settings.settings.adobe.prefer_com,
            "allow_ui_automation": self.settings.settings.adobe.allow_ui_automation,
            "allow_input_automation": self.settings.settings.adobe.allow_input_automation,
        }

    def shutdown(self) -> None:
        """Disconnect both controllers."""
        if self._indesign is not None:
            self._indesign.disconnect()
        if self._photoshop is not None:
            self._photoshop.disconnect()
