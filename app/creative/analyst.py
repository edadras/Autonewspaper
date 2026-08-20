"""Turning attached references into a style brief.

This is where measurement and judgement meet, and the rule that governs it is
simple: the pixels win. The vision model contributes what it is genuinely
better at - what a reference evokes, which tradition it belongs to, what a
designer working in that spirit would avoid - and is shown the measurements
so it works from them. Anything it says about a measurable quantity is
checked, and where it disagrees the measurement stands and the disagreement is
recorded, so nobody is left wondering which to believe.

With no vision model configured the brief is still produced, from the
measurements alone, and says so. That is not a degraded mode anyone has to
apologise for: most of what a reference decides is measurable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.creative.reference import Measurements, measure_image
from app.creative.style import StyleBrief, brief_from_measurements, merge_briefs
from app.creative.video_reference import (
    VIDEO_SUFFIXES,
    VideoMeasurements,
    VideoReferenceError,
    ffmpeg_available,
    measure_video,
)

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif"}

#: The words the model may use for a weight, so a free-text answer cannot put
#: something meaningless into the design.
_WEIGHTS = ("light", "regular", "medium", "bold", "black")


@dataclass
class Reference:
    """One thing the user attached, and what it turned out to mean."""

    path: str
    kind: str = "image"
    measurements: Measurements | None = None
    video: VideoMeasurements | None = None
    brief: StyleBrief | None = None
    error: str = ""

    @property
    def usable(self) -> bool:
        """Whether anything could be read from it."""
        return self.brief is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "path": self.path,
            "kind": self.kind,
            "measurements": (self.video.to_dict() if self.video else None)
            or (self.measurements.to_dict() if self.measurements else None),
            "brief": self.brief.to_dict() if self.brief else None,
            "error": self.error,
        }


@dataclass
class ReferenceSet:
    """Everything attached to one request, and the brief they add up to."""

    references: list[Reference] = field(default_factory=list)
    brief: StyleBrief = field(default_factory=StyleBrief)

    @property
    def usable(self) -> list[Reference]:
        """The references that could be read."""
        return [reference for reference in self.references if reference.usable]

    @property
    def failures(self) -> list[Reference]:
        """The ones that could not be, with the reason."""
        return [reference for reference in self.references if not reference.usable]

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "references": [reference.to_dict() for reference in self.references],
            "brief": self.brief.to_dict(),
        }


class ReferenceAnalyst:
    """Reads references and produces the brief a design should inherit."""

    def __init__(
        self,
        ai: Any = None,
        *,
        use_vision_model: bool = True,
        keyframe_dir: Path | None = None,
    ) -> None:
        self.ai = ai
        self.use_vision_model = use_vision_model and ai is not None
        #: Where a clip's representative frame is kept, so it can be looked at
        #: as well as measured. Falls back to a directory of its own.
        self.keyframe_dir = keyframe_dir

    # ---------------------------------------------------------------- entry
    def analyze(
        self,
        paths: list[Path | str],
        *,
        instruction: str = "",
        product_type: str = "poster",
        language: str = "fa",
    ) -> ReferenceSet:
        """Read every attachment and merge them into one brief."""
        out = ReferenceSet()
        for path in paths:
            out.references.append(
                self.analyze_one(path, instruction=instruction, product_type=product_type, language=language)
            )
        briefs = [reference.brief for reference in out.usable if reference.brief]
        out.brief = merge_briefs(briefs) if briefs else StyleBrief()
        if out.failures:
            log.warning(
                "%d reference(s) could not be read: %s",
                len(out.failures),
                [f"{Path(r.path).name}: {r.error}" for r in out.failures],
            )
        return out

    def analyze_one(
        self,
        path: Path | str,
        *,
        instruction: str = "",
        product_type: str = "poster",
        language: str = "fa",
    ) -> Reference:
        """Read one attachment."""
        source = Path(path)
        reference = Reference(path=str(source))
        if not source.exists():
            reference.error = "the file does not exist"
            return reference

        suffix = source.suffix.lower()
        try:
            if suffix in VIDEO_SUFFIXES:
                reference.kind = "video"
                if not ffmpeg_available():
                    raise VideoReferenceError(
                        "ffmpeg is not installed, so frames cannot be taken from this clip"
                    )
                reference.video = measure_video(source, keep_frame_in=self._keyframes(source))
                reference.measurements = reference.video.average
                summary = reference.video.describe()
            elif suffix in IMAGE_SUFFIXES:
                reference.kind = "image"
                reference.measurements = measure_image(source)
                summary = reference.measurements.describe()
            else:
                reference.error = f"'{suffix}' is not a picture or a clip this can read"
                return reference
        except Exception as exc:  # noqa: BLE001 - one bad reference is not fatal
            reference.error = str(exc)
            log.warning("Could not read the reference %s: %s", source.name, exc)
            return reference

        assert reference.measurements is not None
        brief = brief_from_measurements(reference.measurements, source=source.name)
        if reference.video is not None:
            brief.motion = self._motion_from(reference.video)
        if self.use_vision_model:
            self._add_judgement(
                brief,
                reference,
                summary=summary,
                instruction=instruction,
                product_type=product_type,
                language=language,
            )
        else:
            brief.notes = f"{brief.notes} (measured only; no vision model is configured)"
        reference.brief = brief
        return reference

    # ------------------------------------------------------------ judgement
    def _add_judgement(
        self,
        brief: StyleBrief,
        reference: Reference,
        *,
        summary: str,
        instruction: str,
        product_type: str,
        language: str,
    ) -> None:
        """Ask the vision model for the part that is not measurable."""
        assert reference.measurements is not None
        preview = self._preview_path(reference)
        if preview is None:
            return
        try:
            request = self.ai.prompts.build_request(
                "creative/reference",
                kind=reference.kind,
                product_type=product_type,
                language=language,
                brief_line=(
                    f"The client said: {instruction.strip()}"
                    if instruction.strip()
                    else "No instruction was given."
                ),
                measurements_json=json.dumps(reference.measurements.to_dict(), ensure_ascii=False, indent=1),
                measured_summary=summary,
            )
            answer = self.ai.vision(request, [preview])
            payload = answer.json(required=False) or {}
        except Exception as exc:  # noqa: BLE001 - judgement is optional
            log.warning("The vision model could not read %s: %s", Path(reference.path).name, exc)
            brief.notes = f"{brief.notes} (measured only; the vision model was unavailable)"
            return
        self._apply(brief, payload, reference.measurements)

    def _apply(self, brief: StyleBrief, payload: dict[str, Any], measurements: Measurements) -> None:
        """Take what the model offered, keeping the measurements authoritative."""
        brief.mood = [str(word)[:40] for word in (payload.get("mood") or [])][:5]
        brief.avoid = [str(word)[:80] for word in (payload.get("avoid") or [])][:6]
        brief.era = str(payload.get("era") or "")[:120]
        if not brief.motion:
            brief.motion = str(payload.get("motion") or "")[:200]

        typography = payload.get("typography") or {}
        weight = str(typography.get("display_weight") or "").lower()
        if weight in _WEIGHTS:
            brief.typography.display_weight = weight  # type: ignore[assignment]
        if isinstance(typography.get("serif"), bool):
            brief.typography.serif = typography["serif"]
        if isinstance(typography.get("all_caps"), bool):
            brief.typography.all_caps = typography["all_caps"]

        character = str(typography.get("character") or "")[:200]
        composition = str(payload.get("composition") or "")[:200]
        extra = "; ".join(part for part in (character, composition) if part)
        if extra:
            brief.notes = f"{brief.notes}. {extra}" if brief.notes else extra

        stated = str(payload.get("disagreement") or "").strip()
        if stated:
            brief.disagreements.append(stated[:240])
        brief.disagreements.extend(self._contradictions(payload, measurements))

    @staticmethod
    def _contradictions(payload: dict[str, Any], measurements: Measurements) -> list[str]:
        """Where the model's words disagree with what was measured.

        A model that calls a measurably cold picture "warm and earthy" is not
        adding judgement, it is guessing - and the design should not inherit
        the guess.
        """
        found: list[str] = []
        words = " ".join(
            [
                " ".join(str(word) for word in (payload.get("mood") or [])),
                str(payload.get("composition") or ""),
                str((payload.get("typography") or {}).get("character") or ""),
            ]
        ).lower()
        checks = (
            (("warm", "golden", "sunlit", "earthy"), measurements.warmth < -12, "warm", "measurably cool"),
            (("cool", "cold", "icy", "steely"), measurements.warmth > 12, "cool", "measurably warm"),
            (("dark", "moody", "shadowy"), measurements.brightness > 68, "dark", "measurably bright"),
            (("bright", "airy", "luminous"), measurements.brightness < 35, "bright", "measurably dark"),
            (("minimal", "sparse", "empty"), measurements.busyness > 30, "minimal", "measurably busy"),
            (("busy", "dense", "cluttered"), measurements.busyness < 9, "busy", "measurably sparse"),
        )
        for terms, contradicted, said, measured in checks:
            if contradicted and any(term in words for term in terms):
                found.append(f"the model called it '{said}' but it is {measured}")
        return found

    @staticmethod
    def _motion_from(video: VideoMeasurements) -> str:
        """How a matching edit should move, from the measured pace."""
        pace = (
            "quick cuts on the beat"
            if video.cut_rate > 0.5
            else "long held shots"
            if video.cut_rate < 0.12
            else "an even cutting rhythm"
        )
        movement = (
            "a locked-off camera"
            if video.motion < 8
            else "constant camera movement"
            if video.motion > 35
            else "slow deliberate moves"
        )
        drift = " with a grade that shifts across the piece" if video.grade_drift > 18 else ""
        return f"{pace}, {movement}{drift}"

    def _keyframes(self, source: Path) -> Path:
        """Where a clip's kept frame goes."""
        return self.keyframe_dir or source.parent / ".ains-keyframes"

    @staticmethod
    def _preview_path(reference: Reference) -> Path | None:
        """The frame the vision model is shown."""
        if reference.kind == "image":
            return Path(reference.path)
        if reference.video and reference.video.keyframe:
            frame = Path(reference.video.keyframe)
            return frame if frame.exists() else None
        return None
