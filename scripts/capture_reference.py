"""Capture a reference image for the vision-based automation fallback.

The vision fallback locates a control by matching a picture of it. This helper
grabs a screen region and stores it under the reference directory so the
fallback can find that control later.

Usage:
    python scripts/capture_reference.py export_button 400 250 520 290
    python scripts/capture_reference.py --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automation.vision_automation import VisionAutomation  # noqa: E402
from app.config.paths import get_paths  # noqa: E402


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Capture a UI reference image")
    parser.add_argument("name", nargs="?", help="Name of the control")
    parser.add_argument("left", nargs="?", type=int)
    parser.add_argument("top", nargs="?", type=int)
    parser.add_argument("right", nargs="?", type=int)
    parser.add_argument("bottom", nargs="?", type=int)
    parser.add_argument("--list", action="store_true", help="List the stored references")
    parser.add_argument("--dir", type=Path, help="Reference directory override")
    args = parser.parse_args()

    directory = args.dir or (get_paths().data / "references")
    automation = VisionAutomation(directory, enabled=True)

    if args.list:
        references = automation.references()
        print(f"{len(references)} reference(s) in {directory}")
        for name in references:
            print(f"  {name}")
        return 0

    if not all(
        [
            args.name,
            args.left is not None,
            args.top is not None,
            args.right is not None,
            args.bottom is not None,
        ]
    ):
        parser.error("give a name and the region: NAME LEFT TOP RIGHT BOTTOM")

    try:
        path = automation.capture_reference(args.name, (args.left, args.top, args.right, args.bottom))
    except Exception as exc:  # noqa: BLE001
        print(f"Capture failed: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
