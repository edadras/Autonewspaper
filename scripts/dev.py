"""Developer helper: run checks, the test suite and the application.

Usage:
    python scripts/dev.py check      # ruff + mypy when installed
    python scripts/dev.py test       # the whole suite (offscreen Qt)
    python scripts/dev.py run        # start the desktop application
    python scripts/dev.py diagnose   # print the diagnostics report
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], **env: str) -> int:
    """Run a command from the project root."""
    print("+", " ".join(command), flush=True)
    environment = {**os.environ, **env}
    return subprocess.call(command, cwd=str(ROOT), env=environment)


def check() -> int:
    """Lint and type-check what is installed."""
    code = 0
    if subprocess.call([sys.executable, "-c", "import ruff"], stderr=subprocess.DEVNULL) == 0:
        code |= run([sys.executable, "-m", "ruff", "check", "app", "tests", "scripts"])
        code |= run([sys.executable, "-m", "ruff", "format", "--check", "app"])
    else:
        print("  ruff is not installed; skipping the lint step")
    if subprocess.call([sys.executable, "-c", "import mypy"], stderr=subprocess.DEVNULL) == 0:
        code |= run([sys.executable, "-m", "mypy", "app"])
    else:
        print("  mypy is not installed; skipping the type check")
    return code


def test(extra: list[str]) -> int:
    """Run pytest with Qt in offscreen mode."""
    return run(
        [sys.executable, "-m", "pytest", *extra] if extra else [sys.executable, "-m", "pytest"],
        QT_QPA_PLATFORM="offscreen",
    )


def main() -> int:
    """Entry point."""
    command = sys.argv[1] if len(sys.argv) > 1 else "test"
    extra = sys.argv[2:]
    if command == "check":
        return check()
    if command == "test":
        return test(extra)
    if command == "run":
        return run([sys.executable, "-m", "app.main", *extra])
    if command == "diagnose":
        return run([sys.executable, "-m", "app.main", "--diagnostics"])
    if command == "templates":
        return run([sys.executable, "scripts/build_builtin_templates.py"])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
