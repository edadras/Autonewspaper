"""Vision-based UI automation (the bottom of the chain).

Locates a control on screen by matching a reference image, so an operation can
still be completed when the accessibility tree does not expose the control.
It is the least reliable mechanism in the system and is only reached after
every scripting path and UI Automation have failed.

Matching uses OpenCV when it is installed and a pure-Pillow coarse-to-fine
search otherwise, so the feature does not add a hard dependency.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from app.core.errors import AutomationError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

try:  # pragma: no cover - optional
    import cv2  # type: ignore
    import numpy as _np  # type: ignore

    _CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _np = None  # type: ignore
    _CV2 = False


@dataclass
class Match:
    """A located on-screen control."""

    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        """Centre point, ready for a click."""
        return (self.x + self.width // 2, self.y + self.height // 2)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "x": self.x, "y": self.y, "width": self.width, "height": self.height,
            "confidence": round(self.confidence, 4), "center": self.center,
        }


def grab_screen(region: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Capture the screen (or *region* as ``(left, top, right, bottom)``)."""
    try:  # pragma: no cover - requires a desktop
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=region).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        try:
            import pyautogui  # type: ignore

            shot = pyautogui.screenshot(region=region)
            return shot.convert("RGB")
        except Exception as exc2:  # noqa: BLE001
            raise AutomationError(
                f"Cannot capture the screen: {exc2 or exc}",
                recovery_action="Vision automation needs an interactive desktop session.",
            ) from exc2


def match_template(
    screen: Image.Image, template: Image.Image, threshold: float = 0.86
) -> Match | None:
    """Locate *template* inside *screen*; returns ``None`` below *threshold*."""
    if template.width > screen.width or template.height > screen.height:
        return None
    if _CV2:  # pragma: no cover - requires opencv
        haystack = cv2.cvtColor(_np.array(screen), cv2.COLOR_RGB2GRAY)
        needle = cv2.cvtColor(_np.array(template), cv2.COLOR_RGB2GRAY)
        result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        return Match(int(max_loc[0]), int(max_loc[1]), template.width, template.height, float(max_val))
    return _pillow_match(screen, template, threshold)


def _pillow_match(screen: Image.Image, template: Image.Image, threshold: float) -> Match | None:
    """Coarse-to-fine mean-absolute-difference search without OpenCV.

    The screen and the template are both reduced by the same factor, a coarse
    sweep finds the best candidate, and the match is then refined at full
    resolution around that point.
    """
    scale = max(1, min(screen.width // 480, screen.height // 320, 6))
    small_screen = screen.convert("L").resize(
        (screen.width // scale, screen.height // scale), Image.Resampling.BILINEAR
    )
    small_template = template.convert("L").resize(
        (max(1, template.width // scale), max(1, template.height // scale)), Image.Resampling.BILINEAR
    )
    best_score, best_xy = 1e9, (0, 0)
    step = max(1, small_template.width // 6)
    for y in range(0, small_screen.height - small_template.height + 1, step):
        for x in range(0, small_screen.width - small_template.width + 1, step):
            window = small_screen.crop((x, y, x + small_template.width, y + small_template.height))
            score = ImageStat.Stat(ImageChops.difference(window, small_template)).mean[0]
            if score < best_score:
                best_score, best_xy = score, (x, y)

    origin_x, origin_y = best_xy[0] * scale, best_xy[1] * scale
    grey_screen, grey_template = screen.convert("L"), template.convert("L")
    refined_score, refined_xy = 1e9, (origin_x, origin_y)
    # The refinement window must cover the coarse sweep's step size,
    # otherwise the true peak can sit just outside it.
    span = max(scale * 3, step * scale + scale)
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            x, y = origin_x + dx, origin_y + dy
            if x < 0 or y < 0 or x + template.width > screen.width or y + template.height > screen.height:
                continue
            window = grey_screen.crop((x, y, x + template.width, y + template.height))
            score = ImageStat.Stat(ImageChops.difference(window, grey_template)).mean[0]
            if score < refined_score:
                refined_score, refined_xy = score, (x, y)

    confidence = max(0.0, 1.0 - refined_score / 90.0)
    if confidence < threshold:
        log.debug("Template match rejected: confidence %.3f < %.3f", confidence, threshold)
        return None
    return Match(refined_xy[0], refined_xy[1], template.width, template.height, confidence)


class VisionAutomation:
    """Finds and clicks controls by matching reference images."""

    def __init__(self, reference_dir: Path, *, enabled: bool = False, threshold: float = 0.86) -> None:
        self.reference_dir = Path(reference_dir)
        self.enabled = enabled
        self.threshold = threshold

    def supported(self) -> bool:
        """Whether this mechanism may be used at all."""
        if not self.enabled:
            return False
        try:
            import pyautogui  # type: ignore  # noqa: F401
        except Exception:  # pragma: no cover
            return False
        return True

    def references(self) -> list[str]:
        """Names of the available reference images."""
        if not self.reference_dir.exists():
            return []
        return sorted(p.stem for p in self.reference_dir.glob("*.png"))

    def locate(self, name: str, *, region: tuple[int, int, int, int] | None = None) -> Match | None:
        """Locate the control whose reference image is ``<name>.png``."""
        reference = self.reference_dir / f"{name}.png"
        if not reference.exists():
            raise AutomationError(
                f"No reference image for '{name}'",
                context={"expected": str(reference), "available": self.references()},
                recovery_action="Capture the control with scripts/capture_reference.py.",
            )
        screen = grab_screen(region)
        with Image.open(reference) as template:
            match = match_template(screen, template.convert("RGB"), self.threshold)
        if match:
            offset_x, offset_y = (region[0], region[1]) if region else (0, 0)
            match.x += offset_x
            match.y += offset_y
        return match

    def click(self, name: str, *, region: tuple[int, int, int, int] | None = None) -> bool:
        """Locate a control and click its centre."""
        if not self.supported():
            raise AutomationError(
                "Vision automation is disabled",
                recovery_action="Enable input automation in Adobe Settings to allow it.",
            )
        match = self.locate(name, region=region)
        if match is None:
            return False
        import pyautogui  # type: ignore

        x, y = match.center
        pyautogui.moveTo(x, y, duration=0.12)
        pyautogui.click()
        log.warning("Clicked '%s' at (%d, %d) with confidence %.2f", name, x, y, match.confidence)
        return True

    def wait_for(
        self, name: str, timeout: float = 30.0, interval: float = 1.0
    ) -> Match | None:
        """Wait until a control appears on screen."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = self.locate(name)
            if match:
                return match
            time.sleep(interval)
        return None

    def capture_reference(self, name: str, region: tuple[int, int, int, int]) -> Path:
        """Save a screen region as a new reference image."""
        self.reference_dir.mkdir(parents=True, exist_ok=True)
        target = self.reference_dir / f"{name}.png"
        grab_screen(region).save(target)
        log.info("Saved reference image %s", target)
        return target

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return {
            "enabled": self.enabled,
            "supported": self.supported(),
            "matcher": "opencv" if _CV2 else "pillow",
            "reference_dir": str(self.reference_dir),
            "references": self.references(),
            "threshold": self.threshold,
        }
