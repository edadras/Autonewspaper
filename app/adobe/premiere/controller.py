"""Premiere Pro controller.

Premiere registers no automation object, so unlike InDesign and Photoshop
there is no COM path to try first: every call goes through the CEP extension
over the file-based job queue. That is the same priority-3 mechanism the other
two hosts fall back to, so the plumbing is shared and only the delivery
differs.

What this means in practice, and what the operator has to be told: the panel
has to be installed and Premiere has to be running with it open. The
controller reports that plainly rather than appearing to work and timing out.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.adobe.bridge import AdobeBridge, ScriptResult, working_directory
from app.adobe.detect import AdobeApp, detect_premiere
from app.adobe.jsx import ScriptBuilder
from app.core.errors import AdobeConnectionError, AutomationError
from app.core.events import EventBus, EventType

log = logging.getLogger(__name__)


class PremiereController:
    """Drives Adobe Premiere Pro through the extension panel."""

    def __init__(
        self,
        work_dir: Path,
        *,
        app: AdobeApp | None = None,
        bus: EventBus | None = None,
        script_timeout: float = 900.0,
        launch_timeout: float = 240.0,
    ) -> None:
        self.app = app or detect_premiere()
        self.work_dir = working_directory(Path(work_dir), "premiere")
        self.bridge = AdobeBridge(
            self.app,
            self.work_dir,
            prefer_com=False,
            allow_queue=True,
            default_timeout=script_timeout,
        )
        self.bus = bus
        self.launch_timeout = launch_timeout
        self._connected = False
        self._session: dict[str, Any] | None = None

    # ---------------------------------------------------------- life-cycle
    def available(self) -> bool:
        """Whether Premiere can be reached right now."""
        return self.app.installed and self.bridge.available()

    def connect(self, launch: bool = True) -> str:
        """Connect through the extension, installing it if needed."""
        if self._connected:
            return "queue"
        if not self.app.installed:
            raise AdobeConnectionError(
                "Adobe Premiere Pro is not installed on this machine",
                recovery_action="Install Premiere, or set its path in Adobe Settings.",
            )
        strategy = self.bridge.connect(launch=launch, timeout=self.launch_timeout)
        self._connected = True
        self._emit(EventType.ADOBE_CONNECTED, host="premiere", strategy=strategy.name)
        log.info("Connected to Premiere via '%s'", strategy.name)
        return strategy.name

    def disconnect(self) -> None:
        """Release the connection."""
        self.bridge.shutdown()
        self._connected = False
        self._session = None
        self._emit(EventType.ADOBE_DISCONNECTED, host="premiere")

    shutdown = disconnect

    def session(self) -> dict[str, Any]:
        """Version, project and whether the undocumented QE DOM is present.

        Transitions, effects and speed changes are only reachable through QE.
        Knowing early whether it is there is the difference between planning
        an edit that can be built and one that half can.
        """
        if self._session is None:
            builder = ScriptBuilder("premiere", "setup")
            builder.call("setup", assign="__info")
            builder.emit("__info")
            self._session = dict(self._run(builder.build(), "setup").data or {})
        return self._session

    # ------------------------------------------------------------ project
    def open_project(self, path: Path | str) -> dict[str, Any]:
        """Open an existing Premiere project."""
        builder = ScriptBuilder("premiere", "open_project")
        builder.call("openProject", str(path), assign="__info")
        builder.emit("__info")
        return dict(self._run(builder.build(), "open_project").data or {})

    def create_project(self, path: Path | str) -> dict[str, Any]:
        """Create a project, or open one that is already there."""
        builder = ScriptBuilder("premiere", "create_project")
        builder.call("createProject", str(path), assign="__info")
        builder.emit("__info")
        return dict(self._run(builder.build(), "create_project").data or {})

    def save_project(self, path: Path | str | None = None) -> dict[str, Any]:
        """Save the project, optionally under a new name."""
        builder = ScriptBuilder("premiere", "save_project")
        builder.call("saveProject", str(path) if path else "", assign="__info")
        builder.emit("__info")
        return dict(self._run(builder.build(), "save_project").data or {})

    def project_info(self) -> dict[str, Any]:
        """What the open project contains."""
        builder = ScriptBuilder("premiere", "project_info")
        builder.call("projectInfo", assign="__info")
        builder.emit("__info")
        return dict(self._run(builder.build(), "project_info").data or {})

    # ------------------------------------------------------------ footage
    def import_files(self, paths: list[Path | str], bin_name: str = "Footage") -> dict[str, Any]:
        """Import footage into a bin."""
        missing = [str(p) for p in paths if not Path(p).exists()]
        if missing:
            raise AutomationError(
                f"{len(missing)} file(s) could not be imported because they do not exist",
                context={"missing": missing[:10]},
                recovery_action="Check the paths, or re-link the footage.",
            )
        builder = ScriptBuilder("premiere", "import_files")
        builder.call("importFiles", [str(p) for p in paths], bin_name, assign="__info")
        builder.emit("__info")
        return dict(self._run(builder.build(), "import_files").data or {})

    # ---------------------------------------------------------- the edit
    def build_edit(self, plan: Any) -> dict[str, Any]:
        """Build a whole edit in one script.

        Each step reports its own outcome, and a step that fails does not
        throw the rest of the edit away - a transition Premiere will not add
        should not cost the operator the cuts that did work.
        """
        payload = plan.to_premiere() if hasattr(plan, "to_premiere") else dict(plan)
        builder = ScriptBuilder("premiere", "build_edit")
        builder.call("setup")
        builder.raw("AINS.PPRO.reset();")
        builder.var("__plan", payload)
        builder.raw("var __result = AINS.PPRO.buildEdit(__plan);")
        builder.emit("__result")
        self._emit(
            EventType.ADOBE_COMMAND,
            host="premiere",
            command="build_edit",
            steps=len(payload.get("steps") or []),
        )
        result = self._run(builder.build(), "build_edit")
        data = dict(result.data or {})
        data["strategy"] = result.strategy
        failed = data.get("failed") or []
        if failed:
            log.warning(
                "%d edit step(s) could not be built: %s",
                len(failed),
                [f"{f['action']}#{f['index']}" for f in failed],
            )
        return data

    def timeline_report(self) -> dict[str, Any]:
        """What the timeline actually contains, for the quality check."""
        builder = ScriptBuilder("premiere", "timeline_report")
        builder.call("timelineReport", assign="__report")
        builder.emit("__report")
        return dict(self._run(builder.build(), "timeline_report").data or {})

    def available_effects(self, contains: str = "") -> list[str]:
        """Effect names this installation actually has."""
        builder = ScriptBuilder("premiere", "effects")
        builder.call("availableEffects", contains, assign="__names")
        builder.emit("__names")
        data = self._run(builder.build(), "available_effects").data
        return [str(name) for name in (data or [])]

    # -------------------------------------------------------------- output
    def export_sequence(
        self,
        target: Path | str,
        *,
        preset: Path | str | None = None,
        queue: bool = True,
        work_area: bool = False,
    ) -> dict[str, Any]:
        """Render the active sequence, through Media Encoder by default."""
        spec = {
            "path": str(target),
            "preset": str(preset) if preset else "",
            "queue": queue,
            "work_area": work_area,
        }
        builder = ScriptBuilder("premiere", "export_sequence")
        builder.var("__spec", spec)
        builder.raw("var __out = AINS.PPRO.exportSequence(__spec);")
        builder.emit("__out")
        return dict(self._run(builder.build(), "export_sequence").data or {})

    def export_frame(self, target: Path | str, at: float = 0.0) -> Path:
        """Write a single frame as a PNG - what QA looks at."""
        builder = ScriptBuilder("premiere", "export_frame")
        builder.var("__spec", {"path": str(target), "at": at})
        builder.raw("var __out = AINS.PPRO.exportFrame(__spec);")
        builder.emit("__out")
        self._run(builder.build(), "export_frame")
        return Path(target)

    # ------------------------------------------------------------ plumbing
    def _run(self, script: Any, operation: str) -> ScriptResult:
        if not self._connected:
            self.connect()
        return self.bridge.run(script, operation=operation).raise_for_status(operation)

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return {
            **self.bridge.describe(),
            "extension_dir": str(self.app.scripts_dir) if self.app.scripts_dir else None,
            "note": "Premiere is scripted through its extension panel; there is no COM path.",
        }
