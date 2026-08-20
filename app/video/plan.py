"""What an edit is, before Premiere has been asked to build it.

An :class:`EditPlan` is to a sequence what a layout plan is to a page: a
complete description that can be checked, costed and reviewed before anything
is built, and that Premiere turns into a real timeline.

Times are in seconds throughout. Premiere's own ticks never appear outside the
scripting library.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.formats import Format


class StepKind(str, Enum):
    """What one instruction in an edit does."""

    CLIP = "clip"
    """Place a piece of footage on a video track."""
    AUDIO = "audio"
    """Place a sound on an audio track."""
    TRANSITION = "transition"
    """Put a transition on a cut."""
    EFFECT = "effect"
    """Apply an effect to a clip and set its parameters."""
    TRANSFORM = "transform"
    """Scale, move, rotate or fade a clip, with or without keyframes."""
    OVERLAY = "overlay"
    """Composite a still - usually a Photoshop design - over the footage."""
    SPEED = "speed"
    """Change a clip's playback rate."""


class Step(BaseModel):
    """One instruction in an edit.

    Unknown fields are rejected rather than ignored: a misspelt instruction
    that is silently dropped is exactly the kind of thing that produces an
    edit which is subtly not what was asked for.
    """

    model_config = ConfigDict(extra="forbid")

    action: StepKind
    track: int = 0
    at: float | None = None
    """When, in seconds. ``None`` means "after whatever is already there"."""
    duration: float | None = None

    # --- footage ---------------------------------------------------------
    item: str = ""
    """Name of the project item, as imported."""
    in_point: float | None = None
    out_point: float | None = None

    # --- transitions and effects ----------------------------------------
    clip: int = 0
    """Index of the clip on the track this step acts on."""
    after_clip: int = 0
    """For a transition: which cut it goes on, counted in clips."""
    clip_name: str = ""
    transition: str = ""
    effect: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)

    # --- transform -------------------------------------------------------
    position: list[float] | None = None
    scale: float | None = None
    rotation: float | None = None
    opacity: float | None = None
    anchor: list[float] | None = None

    # --- overlay ---------------------------------------------------------
    path: str = ""
    fade: float | None = None
    bin: str = ""

    # --- speed -----------------------------------------------------------
    factor: float = 1.0

    note: str = ""
    """Why this step is here, in the operator's language."""

    #: Which fields each action actually uses. A zero is meaningful for some
    #: of them (clip index 0 is the first clip), so the shape is decided by
    #: the action rather than by whether a value looks empty.
    FIELDS_BY_ACTION: ClassVar[dict[StepKind, tuple[str, ...]]] = {
        StepKind.CLIP: ("item", "at", "in_point", "out_point", "duration"),
        StepKind.AUDIO: ("item", "at", "duration"),
        StepKind.TRANSITION: ("transition", "after_clip", "duration"),
        StepKind.EFFECT: ("effect", "clip", "parameters"),
        StepKind.TRANSFORM: ("clip", "at", "position", "scale", "rotation", "opacity", "anchor"),
        StepKind.OVERLAY: ("path", "at", "duration", "fade", "bin"),
        StepKind.SPEED: ("clip_name", "factor"),
    }

    def to_premiere(self) -> dict[str, Any]:
        """The shape ``AINS.PPRO.buildEdit`` expects."""
        payload: dict[str, Any] = {"action": self.action.value, "track": self.track}
        for name in self.FIELDS_BY_ACTION.get(self.action, ()):
            value = getattr(self, name)
            if value in (None, "", [], {}):
                continue
            payload[name] = value
        # A transition on the very first cut, or an effect on the first clip,
        # is index zero - which the loop above drops as "empty".
        if self.action is StepKind.TRANSITION:
            payload["after_clip"] = self.after_clip
        elif self.action in (StepKind.EFFECT, StepKind.TRANSFORM):
            payload["clip"] = self.clip
        return payload


class SequenceSpec(BaseModel):
    """The sequence an edit is built in."""

    name: str = "Sequence"
    width: int = 1920
    height: int = 1080
    fps: float = 25.0
    video_tracks: int = 3
    audio_tracks: int = 2
    format_id: str = ""

    @classmethod
    def for_format(cls, item: Format, name: str = "") -> SequenceSpec:
        """A sequence sized for *item*."""
        return cls(
            name=name or item.name,
            width=item.width_px,
            height=item.height_px,
            fps=item.fps or 25.0,
            format_id=item.id,
        )

    def to_premiere(self) -> dict[str, Any]:
        """The shape ``AINS.PPRO.createSequence`` expects."""
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_tracks": self.video_tracks,
            "audio_tracks": self.audio_tracks,
        }


class EditPlan(BaseModel):
    """A complete edit, ready to be built."""

    project_id: int = 0
    name: str = ""
    sequence: SequenceSpec
    footage: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    bin: str = "Footage"
    brief: str = ""
    language: str = "fa"
    safe_area: dict[str, float] | None = None
    """Where a platform's own interface sits, as fractions of the frame."""
    export_preset: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_format(cls, item: Format, *, name: str = "", language: str = "fa") -> EditPlan:
        """An empty edit sized for *item*."""
        safe = item.safe_area
        return cls(
            name=name or item.name,
            language=language,
            sequence=SequenceSpec.for_format(item, name or item.name),
            safe_area=None
            if safe.is_empty()
            else {"top": safe.top, "bottom": safe.bottom, "left": safe.left, "right": safe.right},
        )

    def add(self, step: Step) -> Step:
        """Append a step."""
        self.steps.append(step)
        return step

    def add_clip(
        self,
        item: str,
        *,
        track: int = 0,
        at: float | None = None,
        in_point: float | None = None,
        out_point: float | None = None,
        note: str = "",
    ) -> Step:
        """Place a piece of footage, registering it as needed."""
        if item not in self.footage and Path(item).suffix:
            self.footage.append(item)
        return self.add(
            Step(
                action=StepKind.CLIP,
                item=Path(item).name if Path(item).suffix else item,
                track=track,
                at=at,
                in_point=in_point,
                out_point=out_point,
                note=note,
            )
        )

    @property
    def duration(self) -> float:
        """How long the edit runs, from the steps that carry a length."""
        end = 0.0
        cursor: dict[int, float] = {}
        for step in self.steps:
            if step.action not in (StepKind.CLIP, StepKind.OVERLAY, StepKind.AUDIO):
                continue
            start = step.at if step.at is not None else cursor.get(step.track, 0.0)
            length = step.duration
            if length is None and step.in_point is not None and step.out_point is not None:
                length = max(0.0, step.out_point - step.in_point)
            length = length or 0.0
            cursor[step.track] = start + length
            end = max(end, cursor[step.track])
        return round(end, 3)

    def to_premiere(self) -> dict[str, Any]:
        """The whole plan in the shape ``AINS.PPRO.buildEdit`` expects."""
        return {
            "sequence": self.sequence.to_premiere(),
            "footage": self.footage,
            "bin": self.bin,
            "steps": [step.to_premiere() for step in self.steps],
        }

    def save(self, path: Path | str) -> Path:
        """Write the plan as JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: Path | str) -> EditPlan:
        """Read a plan back."""
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


ExportMode = Literal["direct", "queue"]
