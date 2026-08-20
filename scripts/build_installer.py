"""Build the Windows installer.

Runs the whole chain: checks, PyInstaller bundle, then Inno Setup. Each step
reports what it did, and a missing Inno Setup stops with a clear message
rather than a traceback.

Usage:
    python scripts/build_installer.py            # bundle + installer
    python scripts/build_installer.py --bundle   # bundle only
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "AINewspaperStudio"
SPEC = ROOT / "installer" / "ai_newspaper_studio.spec"
ISS = ROOT / "installer" / "setup.iss"
OUTPUT = ROOT / "installer" / "output"

INNO_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def run(command: list[str], cwd: Path | None = None) -> int:
    """Run a command, echoing it first."""
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.call(command, cwd=str(cwd or ROOT))


def preflight() -> int:
    """Verify the build inputs exist."""
    problems: list[str] = []
    # This script may be launched by any interpreter on the build machine,
    # so the version really does have to be checked at run time.
    if sys.version_info < (3, 11):  # noqa: UP036
        problems.append(f"Python 3.11+ is required (found {sys.version.split()[0]})")
    for path in (SPEC, ROOT / "app" / "main.py", ROOT / "prompts", ROOT / "templates"):
        if not path.exists():
            problems.append(f"missing {path.relative_to(ROOT)}")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("PyInstaller is not installed (pip install pyinstaller)")
    try:
        import PySide6  # noqa: F401
    except ImportError:
        problems.append("PySide6 is not installed")
    if not list((ROOT / "templates").glob("*.template.json")):
        problems.append("no templates found; run scripts/build_builtin_templates.py")
    for problem in problems:
        print(f"  ! {problem}")
    return 1 if problems else 0


def build_bundle(clean: bool) -> int:
    """Run PyInstaller."""
    if clean and DIST.exists():
        shutil.rmtree(DIST, ignore_errors=True)
    command = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"]
    if clean:
        command.append("--clean")
    code = run(command)
    if code == 0:
        executable = DIST / "AINewspaperStudio.exe"
        print(f"  bundle: {DIST}")
        print(f"  entry : {executable} ({'present' if executable.exists() else 'MISSING'})")
    return code


def find_inno() -> Path | None:
    """Locate the Inno Setup compiler."""
    which = shutil.which("iscc") or shutil.which("ISCC")
    if which:
        return Path(which)
    env = os.environ.get("INNO_SETUP")
    if env and Path(env).exists():
        return Path(env)
    return next((path for path in INNO_CANDIDATES if path.exists()), None)


def build_installer() -> int:
    """Run Inno Setup over the bundle."""
    if not DIST.exists():
        print("  ! the PyInstaller bundle is missing; run the bundle step first")
        return 1
    compiler = find_inno()
    if compiler is None:
        print(
            "  ! Inno Setup 6 was not found.\n"
            "    Install it from https://jrsoftware.org/isdl.php, or set INNO_SETUP\n"
            "    to the full path of ISCC.exe. The PyInstaller bundle in dist/ is\n"
            "    already usable without an installer."
        )
        return 1
    OUTPUT.mkdir(parents=True, exist_ok=True)
    code = run([str(compiler), str(ISS)], cwd=ROOT / "installer")
    if code == 0:
        produced = sorted(OUTPUT.glob("*.exe"))
        for path in produced:
            print(f"  installer: {path}  ({path.stat().st_size / 1024 / 1024:.1f} MB)")
    return code


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Build the Windows installer")
    parser.add_argument("--bundle", action="store_true", help="Only build the PyInstaller bundle")
    parser.add_argument("--installer", action="store_true", help="Only run Inno Setup")
    parser.add_argument("--clean", action="store_true", help="Rebuild from scratch")
    args = parser.parse_args()

    print("== pre-flight ==")
    if preflight():
        return 1
    print("  ok")

    if not args.installer:
        print("== PyInstaller ==")
        if build_bundle(args.clean):
            return 1
    if not args.bundle:
        print("== Inno Setup ==")
        return build_installer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
