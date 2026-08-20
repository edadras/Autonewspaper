"""Adobe execution bridge.

Implements the automation priority chain of specification §2. An operation is
expressed once, as an ExtendScript program, and the bridge decides *how* to
get it into the host application:

======  ==========================  ===========================================
1       :class:`ComStrategy`        COM scripting API (``DoScript``)
2       :class:`ScriptFileStrategy` COM with the script passed as a file
3       :class:`QueueStrategy`      resident queue runner + Scripts Panel
4       UI Automation               :mod:`app.automation.ui_automation`
5       Input automation            :mod:`app.automation.input_automation`
======  ==========================  ===========================================

Strategies 4 and 5 cannot execute arbitrary scripts - they are used by the
controllers for the few operations that have a UI-only path - so the bridge
exposes them through :meth:`AdobeBridge.fallback_chain` rather than running
JSX through them.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adobe.detect import AdobeApp
from app.adobe.jsx import Script
from app.core.errors import AdobeConnectionError, ScriptExecutionError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

#: InDesign's ``ScriptLanguage.JAVASCRIPT`` enumerator.
SCRIPT_LANGUAGE_JAVASCRIPT = 1246973031
#: Photoshop's ``DialogModes.NO``.
PS_DIALOG_NO = 3


@dataclass
class ScriptResult:
    """Outcome of running one script."""

    ok: bool
    data: Any = None
    error: dict[str, Any] | None = None
    log: list[str] = field(default_factory=list)
    strategy: str = ""
    duration: float = 0.0
    raw: str = ""

    @classmethod
    def parse(cls, payload: str, strategy: str, duration: float) -> ScriptResult:
        """Parse the JSON a script returned (or wrote to its result file)."""
        text = (payload or "").strip()
        if not text:
            return cls(
                ok=False,
                error={"message": "Script returned no output"},
                strategy=strategy,
                duration=duration,
            )
        try:
            data = json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return cls(
                    ok=False,
                    error={"message": f"Unparsable script output: {text[:300]}"},
                    strategy=strategy,
                    duration=duration,
                    raw=text,
                )
            try:
                data = json.loads(text[start : end + 1])
            except ValueError as exc:
                return cls(
                    ok=False,
                    error={"message": f"Unparsable script output: {exc}"},
                    strategy=strategy,
                    duration=duration,
                    raw=text,
                )
        return cls(
            ok=bool(data.get("ok")),
            data=data.get("data"),
            error=data.get("error"),
            log=list(data.get("log") or []),
            strategy=strategy,
            duration=duration,
            raw=text,
        )

    def message(self) -> str:
        """Human readable error message."""
        if self.ok:
            return "ok"
        if isinstance(self.error, dict):
            return str(self.error.get("message") or self.error)
        return str(self.error or "unknown error")

    def raise_for_status(self, operation: str) -> ScriptResult:
        """Raise :class:`ScriptExecutionError` when the script failed."""
        if not self.ok:
            raise ScriptExecutionError(
                f"{operation} failed: {self.message()}",
                context={"strategy": self.strategy, "log": self.log[-12:], "error": self.error},
            )
        return self


class ExecutionStrategy:
    """Base class for the ways a script can reach the host application."""

    name = "abstract"
    priority = 99

    def __init__(self, app: AdobeApp) -> None:
        self.app = app

    def available(self) -> bool:
        """Whether this strategy can be used right now."""
        return False

    def execute(self, script: Script, timeout: float) -> ScriptResult:
        """Run *script* and return its result."""
        raise NotImplementedError

    def launch(self, timeout: float) -> bool:
        """Start the host application if it is not already running."""
        if not self.app.executable or not self.app.executable.exists():
            return False
        try:
            subprocess.Popen(
                [str(self.app.executable)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:  # pragma: no cover - depends on the machine
            log.error("Cannot launch %s: %s", self.app.executable, exc)
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.available():
                return True
            time.sleep(1.0)
        return self.available()


class ComStrategy(ExecutionStrategy):
    """Priority 1 - the official COM scripting API."""

    name = "com"
    priority = 1

    def __init__(self, app: AdobeApp) -> None:
        super().__init__(app)
        # COM objects live in the apartment of the thread that created them:
        # a proxy dispatched on the GUI thread raises RPC_E_WRONG_THREAD when
        # the pipeline's worker thread uses it. Each thread therefore keeps
        # its own connection, and initialises COM for itself.
        self._local = threading.local()

    @property
    def _client(self) -> Any:
        return getattr(self._local, "client", None)

    @_client.setter
    def _client(self, value: Any) -> None:
        self._local.client = value

    def _dispatch(self) -> Any:
        """Return a live COM object for the calling thread, connecting on first use."""
        if self._client is not None:
            return self._client
        if not IS_WINDOWS:
            raise AdobeConnectionError("COM automation is only available on Windows")
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError as exc:  # pragma: no cover - Windows only
            raise AdobeConnectionError(
                "pywin32 is not installed; COM automation is unavailable",
                recovery_action="pip install pywin32, or use the file-based automation path.",
                cause=exc,
            ) from exc
        try:
            pythoncom.CoInitialize()
        except Exception:  # pragma: no cover - this thread already initialised COM
            pass
        prog_ids = [self.app.prog_id] if self.app.prog_id else []
        from app.adobe.detect import INDESIGN_PROGIDS, PHOTOSHOP_PROGIDS

        prog_ids += INDESIGN_PROGIDS if self.app.kind == "indesign" else PHOTOSHOP_PROGIDS
        last: Exception | None = None
        for prog_id in dict.fromkeys(p for p in prog_ids if p):
            try:
                self._client = win32com.client.Dispatch(prog_id)
                self.app.prog_id = prog_id
                log.info("Connected to %s via COM (%s)", self.app.kind, prog_id)
                return self._client
            except Exception as exc:  # noqa: BLE001 - try the next ProgID
                last = exc
        raise AdobeConnectionError(
            f"Could not connect to {self.app.kind} over COM: {last}",
            recovery_action="Start the application manually, then retry.",
            cause=last,
        )

    def available(self) -> bool:
        """Whether a COM connection can be established."""
        if not IS_WINDOWS or not self.app.installed:
            return False
        try:
            self._dispatch()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("COM unavailable: %s", exc)
            return False

    def execute(self, script: Script, timeout: float) -> ScriptResult:
        """Run the script through ``DoScript`` / ``DoJavaScript``."""
        started = time.monotonic()
        client = self._dispatch()
        source = script.render()
        try:
            if self.app.kind == "indesign":
                payload = client.DoScript(source, SCRIPT_LANGUAGE_JAVASCRIPT)
            else:
                payload = client.DoJavaScript(source, [], PS_DIALOG_NO)
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - started
            fallback = _read_result_file(script.result_path)
            if fallback:
                return ScriptResult.parse(fallback, self.name, duration)
            raise ScriptExecutionError(
                f"COM DoScript failed for '{script.name}': {exc}",
                context={"host": self.app.kind, "operation": script.name},
                cause=exc,
            ) from exc
        duration = time.monotonic() - started
        text = str(payload) if payload is not None else ""
        if not text.strip():
            text = _read_result_file(script.result_path) or ""
        return ScriptResult.parse(text, self.name, duration)

    def close(self) -> None:
        """Drop this thread's COM reference."""
        self._client = None


class ScriptFileStrategy(ComStrategy):
    """Priority 2 - COM, but the script is passed as a file.

    Long programs and non-ASCII payloads are more reliable as files, and this
    path also works when the automation host refuses very large in-line
    strings.
    """

    name = "script-file"
    priority = 2

    def __init__(self, app: AdobeApp, work_dir: Path) -> None:
        super().__init__(app)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def execute(self, script: Script, timeout: float) -> ScriptResult:
        """Write the script to disk and ask the host to evaluate the file."""
        started = time.monotonic()
        client = self._dispatch()
        path = self.work_dir / f"{script.name}_{uuid.uuid4().hex[:8]}.jsx"
        script.save(path)
        loader = f"var __f = new File({json.dumps(str(path))}); $.evalFile(__f);"
        try:
            if self.app.kind == "indesign":
                payload = client.DoScript(loader, SCRIPT_LANGUAGE_JAVASCRIPT)
            else:
                payload = client.DoJavaScript(loader, [], PS_DIALOG_NO)
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - started
            fallback = _read_result_file(script.result_path)
            if fallback:
                return ScriptResult.parse(fallback, self.name, duration)
            raise ScriptExecutionError(
                f"File-based DoScript failed for '{script.name}': {exc}",
                context={"script": str(path)},
                cause=exc,
            ) from exc
        duration = time.monotonic() - started
        text = str(payload) if payload is not None else ""
        if not text.strip():
            text = _read_result_file(script.result_path) or ""
        return ScriptResult.parse(text, self.name, duration)


class QueueStrategy(ExecutionStrategy):
    """Priority 3 - file-based automation through the resident queue runner.

    A small resident script (``queue_runner.jsx``) is installed into the host's
    Scripts Panel folder and watches a queue directory. Jobs are dropped in as
    files and the results are read back from disk, so no COM connection and no
    UI interaction are needed.
    """

    name = "queue"
    priority = 3

    def __init__(self, app: AdobeApp, work_dir: Path) -> None:
        super().__init__(app)
        self.queue_dir = Path(work_dir) / "queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._installed = False

    def install(self) -> bool:
        """Copy the runner into the Scripts Panel folder and point it here."""
        if self._installed:
            return True
        scripts_dir = self.app.scripts_dir
        if scripts_dir is None:
            log.debug("No Scripts Panel folder for %s; queue automation unavailable", self.app.kind)
            return False
        try:
            scripts_dir.mkdir(parents=True, exist_ok=True)
            source = Path(__file__).resolve().parent / "scripts" / "queue_runner.jsx"
            target_dir = scripts_dir / "AINewspaperStudio"
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_dir / "queue_runner.jsx")
            (target_dir / "queue_path.txt").write_text(str(self.queue_dir), encoding="utf-8")
            (self.queue_dir / "STOP").unlink(missing_ok=True)
            self._installed = True
            log.info("Installed the queue runner into %s", target_dir)
            return True
        except OSError as exc:
            log.warning("Cannot install the queue runner: %s", exc)
            return False

    def available(self) -> bool:
        """Whether the runner is installed and the host is running."""
        if not self.app.installed or not self.install():
            return False
        return _process_running(self.app.executable)

    def execute(self, script: Script, timeout: float) -> ScriptResult:
        """Drop a job in the queue and wait for its result file."""
        started = time.monotonic()
        job_id = f"{script.name}_{uuid.uuid4().hex[:8]}"
        result_file = self.queue_dir / f"{job_id}.result.json"
        script.result_path = result_file
        job_file = self.queue_dir / f"{job_id}.job.jsx"
        tmp = job_file.with_suffix(".tmp")
        tmp.write_text(script.render(), encoding="utf-8")
        tmp.replace(job_file)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result_file.exists():
                text = result_file.read_text(encoding="utf-8", errors="replace")
                result = ScriptResult.parse(text, self.name, time.monotonic() - started)
                result_file.unlink(missing_ok=True)
                return result
            time.sleep(0.25)
        job_file.unlink(missing_ok=True)
        raise ScriptExecutionError(
            f"Queued script '{script.name}' timed out after {timeout:.0f}s",
            context={"queue": str(self.queue_dir)},
            recovery_action="Check that the queue runner is running in the Scripts Panel.",
        )

    def stop(self) -> None:
        """Ask the resident runner to exit."""
        try:
            (self.queue_dir / "STOP").write_text("stop", encoding="utf-8")
        except OSError:  # pragma: no cover
            pass


def _read_result_file(path: Path | None) -> str:
    """Read a script result file if the script managed to write one."""
    if path is None:
        return ""
    file = Path(path)
    if not file.exists():
        return ""
    try:
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return ""


def _process_running(executable: Path | None) -> bool:
    """Whether the given executable currently has a running process."""
    if executable is None:
        return False
    name = executable.name
    if IS_WINDOWS:
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            ).stdout
            return name.lower() in output.lower()
        except Exception:  # noqa: BLE001 - tasklist may be blocked
            return False
    try:
        output = subprocess.run(
            ["pgrep", "-f", name], capture_output=True, text=True, timeout=10, check=False
        )
        return output.returncode == 0
    except Exception:  # noqa: BLE001
        return False


class AdobeBridge:
    """Runs scripts against one Adobe application using the best strategy."""

    def __init__(
        self,
        app: AdobeApp,
        work_dir: Path,
        *,
        prefer_com: bool = True,
        allow_queue: bool = True,
        default_timeout: float = 600.0,
    ) -> None:
        self.app = app
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.default_timeout = default_timeout
        self.strategies: list[ExecutionStrategy] = []
        if prefer_com:
            self.strategies.append(ComStrategy(app))
            self.strategies.append(ScriptFileStrategy(app, self.work_dir / "scripts"))
        if allow_queue:
            self.strategies.append(QueueStrategy(app, self.work_dir))
        self.strategies.sort(key=lambda s: s.priority)
        self.active: ExecutionStrategy | None = None
        self._launch_attempted = False

    # ----------------------------------------------------------- lifecycle
    def connect(self, *, launch: bool = True, timeout: float = 180.0) -> ExecutionStrategy:
        """Pick the highest-priority working strategy, launching if needed."""
        if self.active is not None and self.active.available():
            return self.active
        if not self.app.installed:
            raise AdobeConnectionError(
                f"{self.app.kind} is not installed on this machine",
                recovery_action="Install the application or set its path in Adobe Settings.",
            )
        for strategy in self.strategies:
            try:
                if strategy.available():
                    self.active = strategy
                    log.info("Using the '%s' automation strategy for %s", strategy.name, self.app.kind)
                    return strategy
            except Exception as exc:  # noqa: BLE001
                log.debug("Strategy %s unavailable: %s", strategy.name, exc)

        if launch and not self._launch_attempted:
            self._launch_attempted = True
            log.info("Launching %s ...", self.app.executable)
            for strategy in self.strategies:
                if strategy.launch(timeout):
                    self.active = strategy
                    log.info("Launched and connected via '%s'", strategy.name)
                    return strategy

        raise AdobeConnectionError(
            f"No automation strategy could reach {self.app.kind}",
            context={"strategies": [s.name for s in self.strategies]},
            recovery_action="Start the application manually and run System Diagnostics.",
        )

    def is_connected(self) -> bool:
        """Whether a strategy is currently live."""
        return self.active is not None and self.active.available()

    # -------------------------------------------------------------- execute
    def run(
        self,
        script: Script,
        *,
        timeout: float | None = None,
        operation: str | None = None,
        retries: int = 1,
    ) -> ScriptResult:
        """Execute *script*, degrading through the strategy chain on failure."""
        timeout = timeout or self.default_timeout
        operation = operation or script.name
        if script.result_path is None:
            script.result_path = self.work_dir / "results" / f"{script.name}_{uuid.uuid4().hex[:8]}.json"
            script.result_path.parent.mkdir(parents=True, exist_ok=True)

        self.connect()
        errors: list[str] = []
        start_index = self.strategies.index(self.active) if self.active in self.strategies else 0

        for strategy in self.strategies[start_index:]:
            if not strategy.available():
                continue
            for attempt in range(retries + 1):
                try:
                    result = strategy.execute(script, timeout)
                except Exception as exc:  # noqa: BLE001 - try the next strategy
                    errors.append(f"{strategy.name}: {exc}")
                    log.warning(
                        "Strategy '%s' failed for %s (attempt %d/%d): %s",
                        strategy.name,
                        operation,
                        attempt + 1,
                        retries + 1,
                        exc,
                    )
                    continue
                if result.ok:
                    self.active = strategy
                    for line in result.log:
                        log.debug("[%s] %s", self.app.kind, line)
                    return result
                errors.append(f"{strategy.name}: {result.message()}")
                log.warning("Script '%s' reported an error: %s", operation, result.message())
                # A script that ran but reported an error will report the same
                # error through any other strategy, so stop here.
                return result
        raise ScriptExecutionError(
            f"Every automation strategy failed for '{operation}'",
            context={"errors": errors, "host": self.app.kind},
            recovery_action="Run System Diagnostics; the step can be completed manually.",
        )

    def fallback_chain(self) -> list[str]:
        """Names of the strategies that would be tried, in order."""
        return [s.name for s in self.strategies] + ["ui-automation", "input-automation"]

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return {
            "host": self.app.kind,
            "installed": self.app.installed,
            "executable": str(self.app.executable) if self.app.executable else None,
            "version": self.app.version,
            "prog_id": self.app.prog_id,
            "active_strategy": self.active.name if self.active else None,
            "chain": self.fallback_chain(),
            "work_dir": str(self.work_dir),
        }

    def shutdown(self) -> None:
        """Release COM references and stop the queue runner."""
        for strategy in self.strategies:
            if isinstance(strategy, QueueStrategy):
                strategy.stop()
            close = getattr(strategy, "close", None)
            if callable(close):
                close()
        self.active = None


def working_directory(base: Path, host: str) -> Path:
    """Per-host scratch directory used for scripts, queues and results."""
    path = Path(base) / "adobe" / host
    path.mkdir(parents=True, exist_ok=True)
    return path
