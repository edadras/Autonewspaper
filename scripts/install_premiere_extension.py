"""Install the Premiere Pro extension.

Premiere has no Scripts Panel to drop a resident runner into; its equivalent
is a CEP extension. Unsigned extensions are refused unless the debug flag is
set, which is a per-user setting on the operator's own machine - so this
script is the one place that changes it, it says what it is changing, and it
can be run again to undo it.

    python scripts/install_premiere_extension.py            # install
    python scripts/install_premiere_extension.py --remove   # uninstall
    python scripts/install_premiere_extension.py --check    # report only
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adobe.detect import cep_extensions_dir, detect_premiere  # noqa: E402

EXTENSION_NAME = "AINewspaperStudio"
#: The CSXS versions Premiere has shipped with; the flag is per version.
CSXS_VERSIONS = [f"CSXS.{n}" for n in range(6, 13)]


def source_dir() -> Path:
    """Where the extension lives inside the application."""
    return Path(__file__).resolve().parents[1] / "app" / "adobe" / "cep" / EXTENSION_NAME


def debug_flag(enable: bool) -> list[str]:
    """Allow (or stop allowing) unsigned extensions for this user."""
    messages: list[str] = []
    if sys.platform == "win32":
        import winreg  # noqa: PLC0415 - Windows only

        for version in CSXS_VERSIONS:
            try:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Adobe\{version}")
                with key:
                    if enable:
                        winreg.SetValueEx(key, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
                    else:
                        try:
                            winreg.DeleteValue(key, "PlayerDebugMode")
                        except FileNotFoundError:
                            continue
                messages.append(f"HKCU\\Software\\Adobe\\{version}\\PlayerDebugMode")
            except OSError as exc:  # pragma: no cover - depends on the machine
                messages.append(f"{version}: {exc}")
        return messages
    if sys.platform == "darwin":  # pragma: no cover - macOS convenience
        import subprocess

        for version in CSXS_VERSIONS:
            domain = f"com.adobe.{version}"
            if enable:
                subprocess.run(["defaults", "write", domain, "PlayerDebugMode", "1"], check=False)
            else:
                subprocess.run(["defaults", "delete", domain, "PlayerDebugMode"], check=False)
            messages.append(domain)
        return messages
    return ["Unsigned extensions are a Windows and macOS setting; nothing to change here."]


def install(target_root: Path) -> Path:
    """Copy the extension into place."""
    source = source_dir()
    if not source.exists():
        raise SystemExit(f"The extension is missing from this installation: {source}")
    target = target_root / EXTENSION_NAME
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


def main(argv: list[str] | None = None) -> int:
    """Install, remove or report on the extension."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--remove", action="store_true", help="Uninstall instead of installing")
    parser.add_argument("--check", action="store_true", help="Report the state and change nothing")
    args = parser.parse_args(argv)

    premiere = detect_premiere()
    root = cep_extensions_dir()
    print(
        f"Premiere Pro : {'found' if premiere.installed else 'not found'}"
        f"{' at ' + str(premiere.executable) if premiere.executable else ''}"
    )
    print(f"Extensions   : {root or 'not available on this platform'}")

    if root is None:
        print("\nThis platform has no CEP extensions folder, so Premiere cannot be scripted here.")
        return 1

    target = root / EXTENSION_NAME
    if args.check:
        print(f"Installed    : {'yes' if target.exists() else 'no'} ({target})")
        return 0

    if args.remove:
        if target.exists():
            shutil.rmtree(target)
            print(f"Removed      : {target}")
        else:
            print("Removed      : nothing was installed")
        for entry in debug_flag(False):
            print(f"  cleared {entry}")
        return 0

    root.mkdir(parents=True, exist_ok=True)
    installed = install(root)
    print(f"Installed    : {installed}")
    for entry in debug_flag(True):
        print(f"  set {entry}")
    print(
        "\nStart Premiere Pro and open Window -> Extensions -> AI Newspaper Studio.\n"
        "The panel has to stay open for the application to reach Premiere."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
