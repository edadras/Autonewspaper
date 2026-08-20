"""Reading a clip the user attached.

A moving reference is analysed the same way a still is - the palette, the
contrast, where the weight sits - but sampled across the running time, plus
the things only a clip has: how fast it cuts, how much it moves, whether the
grade drifts from one end to the other.

Frames come from ffmpeg when it is installed, and from OpenCV when it is.
Neither is a dependency of the application, because neither is needed to
produce a newspaper - so when both are missing this says so plainly and the
still-image path still works. It never guesses at a clip it could not open.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import AppError, Component
from app.creative.reference import Measurements, Swatch, assign_roles, measure_image

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv"}


class VideoReferenceError(AppError):
    """A clip could not be read."""

    component = Component.VISION
    recovery_action = "Install ffmpeg and put it on the PATH, or attach a still frame from the clip instead."


@dataclass
class Shot:
    """One sampled moment of a clip."""

    at: float
    measurements: Measurements

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"at": round(self.at, 2), **self.measurements.to_dict()}


@dataclass
class VideoMeasurements:
    """What a clip measures across its running time."""

    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    shots: list[Shot] = field(default_factory=list)
    keyframe: str = ""
    """A frame kept on disk that stands for the clip, for the vision model."""
    palette: list[Swatch] = field(default_factory=list)
    """The palette of the clip as a whole, not of one frame."""
    cut_rate: float = 0.0
    """Detected cuts per second - how fast the edit moves."""
    motion: float = 0.0
    """0-100: how much changes between frames within a shot."""
    grade_drift: float = 0.0
    """0-100: how far the look travels from the first frame to the last."""

    @property
    def average(self) -> Measurements:
        """The clip's measurements averaged over the sampled frames."""
        out = Measurements(width=self.width, height=self.height)
        if not self.shots:
            return out
        count = len(self.shots)
        out.aspect = self.width / max(1, self.height)
        for name in ("brightness", "contrast", "saturation", "warmth", "busyness", "empty_share"):
            setattr(out, name, sum(getattr(s.measurements, name) for s in self.shots) / count)
        out.weight_x = sum(s.measurements.weight_x for s in self.shots) / count
        out.weight_y = sum(s.measurements.weight_y for s in self.shots) / count
        out.is_monochrome = all(s.measurements.is_monochrome for s in self.shots)
        out.has_faces = max(s.measurements.has_faces for s in self.shots)
        out.palette = self.palette
        return out

    def describe(self) -> str:
        """A sentence about the clip, from the numbers alone."""
        pace = "fast-cut" if self.cut_rate > 0.5 else "slow" if self.cut_rate < 0.12 else "steadily cut"
        movement = "static" if self.motion < 8 else "very active" if self.motion > 35 else "moving"
        drift = ", the grade shifts across it" if self.grade_drift > 18 else ""
        return f"{self.average.describe()}; {pace} and {movement}{drift}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "duration": round(self.duration, 2),
            "fps": round(self.fps, 3),
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "cut_rate": round(self.cut_rate, 3),
            "motion": round(self.motion, 1),
            "grade_drift": round(self.grade_drift, 1),
            "average": self.average.to_dict(),
            "shots": [shot.to_dict() for shot in self.shots],
        }


def ffmpeg_available() -> bool:
    """Whether frames can be taken from a clip on this machine."""
    return bool(shutil.which("ffmpeg"))


def probe(path: Path | str) -> dict[str, Any]:
    """Duration, size and frame rate, from ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        output = subprocess.run(  # noqa: S603 - a fixed executable, one file argument
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if output.returncode != 0:
            log.warning("ffprobe could not read %s: %s", path, output.stderr.strip()[:200])
            return {}
        return json.loads(output.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed on %s: %s", path, exc)
        return {}


def extract_frames(path: Path | str, count: int, into: Path) -> list[tuple[float, Path]]:
    """Take *count* evenly spaced frames, returning ``(seconds, file)``."""
    source = Path(path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoReferenceError(f"ffmpeg is not installed, so {source.name} cannot be read")
    info = probe(source)
    duration = _duration(info)
    if duration <= 0:
        raise VideoReferenceError(
            f"{source.name} reports no duration; it may not be a video this machine can read"
        )
    into.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, Path]] = []
    for index in range(count):
        # Sample inside the clip rather than at its very edges, where a fade
        # would be measured instead of the content.
        at = duration * (index + 0.5) / count
        target = into / f"frame_{index:03d}.png"
        result = subprocess.run(  # noqa: S603 - fixed executable, generated paths
            [ffmpeg, "-ss", f"{at:.3f}", "-i", str(source), "-frames:v", "1", "-y", str(target)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and target.exists():
            frames.append((at, target))
        else:
            log.debug("Frame at %.2fs could not be taken: %s", at, result.stderr.strip()[:160])
    if not frames:
        raise VideoReferenceError(f"No frame of {source.name} could be read")
    return frames


def measure_video(
    path: Path | str, *, samples: int = 8, keep_frame_in: Path | None = None
) -> VideoMeasurements:
    """Measure a moving reference across its running time.

    When *keep_frame_in* is given, the most representative frame is kept
    there - a clip that can only be measured and never looked at loses half
    of what a reference is for.
    """
    source = Path(path)
    if not source.exists():
        raise VideoReferenceError(f"The clip does not exist: {source}")
    info = probe(source)
    out = VideoMeasurements(duration=_duration(info), fps=_fps(info))
    streams = info.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    out.width = int(video_stream.get("width") or 0)
    out.height = int(video_stream.get("height") or 0)
    out.has_audio = any(s.get("codec_type") == "audio" for s in streams)

    with tempfile.TemporaryDirectory(prefix="ains-frames-") as scratch:
        frames = extract_frames(source, samples, Path(scratch))
        for at, frame in frames:
            out.shots.append(Shot(at=at, measurements=measure_image(frame, faces=False)))
        if not out.width and out.shots:
            out.width = out.shots[0].measurements.width
            out.height = out.shots[0].measurements.height
        out.palette = _clip_palette(out.shots)
        out.cut_rate, out.motion = _pace(out.shots, out.duration)
        out.grade_drift = _grade_drift(out.shots)
        if keep_frame_in is not None and out.shots:
            out.keyframe = str(_keep_representative(out.shots, frames, keep_frame_in, source))
    log.info("Measured %s: %s", source.name, out.describe())
    return out


def _keep_representative(
    shots: list[Shot], frames: list[tuple[float, Path]], into: Path, source: Path
) -> Path:
    """Copy out the frame that best stands for the clip.

    The one closest to the clip's own averages, rather than the first - an
    opening frame is often a fade, a title card or black.
    """
    import shutil

    average_brightness = sum(s.measurements.brightness for s in shots) / len(shots)
    average_busyness = sum(s.measurements.busyness for s in shots) / len(shots)

    def distance(shot: Shot) -> float:
        return abs(shot.measurements.brightness - average_brightness) + abs(
            shot.measurements.busyness - average_busyness
        )

    best = min(shots, key=distance)
    frame = next((path for at, path in frames if abs(at - best.at) < 1e-6), frames[0][1])
    into.mkdir(parents=True, exist_ok=True)
    target = into / f"{source.stem}_keyframe.png"
    shutil.copy2(frame, target)
    return target


def _duration(info: dict[str, Any]) -> float:
    value = (info.get("format") or {}).get("duration")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fps(info: dict[str, Any]) -> float:
    for stream in info.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        rate = str(stream.get("r_frame_rate") or "0/1")
        try:
            numerator, denominator = (float(part) for part in rate.split("/"))
            return numerator / denominator if denominator else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    return 0.0


def _clip_palette(shots: list[Shot]) -> list[Swatch]:
    """The palette of the whole clip, merged across the sampled frames."""
    totals: dict[str, float] = {}
    for shot in shots:
        for swatch in shot.measurements.palette:
            totals[swatch.hex] = totals.get(swatch.hex, 0.0) + swatch.share
    ordered = sorted(totals.items(), key=lambda item: -item[1])[:6]
    count = max(1, len(shots))
    return assign_roles([Swatch(hex=hex_value, share=share / count) for hex_value, share in ordered])


def _pace(shots: list[Shot], duration: float) -> tuple[float, float]:
    """How often it cuts, and how much moves in between.

    A large jump in brightness, colour temperature and busyness between two
    samples is a cut; a small one is movement. Sampling cannot see every cut,
    so this is a rate rather than a count, and it is reported as such.
    """
    if len(shots) < 2 or duration <= 0:
        return (0.0, 0.0)
    cuts = 0
    movement: list[float] = []
    for previous, current in zip(shots, shots[1:], strict=False):
        a, b = previous.measurements, current.measurements
        change = (
            abs(a.brightness - b.brightness) / 100
            + abs(a.warmth - b.warmth) / 200
            + abs(a.busyness - b.busyness) / 100
        )
        if change > 0.28:
            cuts += 1
        else:
            movement.append(change * 100)
    sampled_span = shots[-1].at - shots[0].at or duration
    return (
        round(cuts / max(1e-6, sampled_span), 3),
        round(sum(movement) / len(movement), 2) if movement else 0.0,
    )


def _grade_drift(shots: list[Shot]) -> float:
    """How far the look travels from the first frame to the last."""
    if len(shots) < 2:
        return 0.0
    first, last = shots[0].measurements, shots[-1].measurements
    return round(
        min(
            100.0,
            abs(first.brightness - last.brightness)
            + abs(first.warmth - last.warmth) / 2
            + abs(first.saturation - last.saturation),
        ),
        2,
    )
