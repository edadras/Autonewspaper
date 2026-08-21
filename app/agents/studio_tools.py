"""The tools the studio's specialists work with.

Specification §55 applies here exactly as it does to the page agents: a
specialist never touches Photoshop, InDesign or Premiere directly. It calls a
registered tool, and the registry validates the arguments, checks the
capability, runs the handler and verifies the result before the specialist is
told anything.

The surface is split by application because that is how the permissions are
split. A Premiere specialist holds ``video.*`` and nothing else, so no amount
of confusion on its part can put a layer into somebody's poster.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adobe.service import AdobeService
from app.agents.tools import (
    Parameter,
    PermissionPolicy,
    Tool,
    ToolRegistry,
    verify_exists,
    verify_truthy,
)
from app.core.errors import ToolValidationError
from app.core.events import EventBus
from app.creative.analyst import ReferenceAnalyst
from app.design.fidelity import DesignFidelity
from app.design.furniture import Furniture, FurnitureFactory, FurnitureSpec
from app.design.plan import Box, DesignPlan, Effects, Layer, LayerKind, ShapeKind
from app.design.renderer import DesignRenderer, fit_to_box, measure_text
from app.formats.registry import FormatRegistry
from app.layout.engine import LayoutEngine
from app.models.schemas import ElementSpec, ElementType, LayoutPlan, PageLayout, Rect
from app.templates.schema import GridSpec, MarginSpec, TemplateSpec
from app.video.plan import EditPlan, Step, StepKind

log = logging.getLogger(__name__)

#: Capabilities the studio adds on top of :class:`PermissionPolicy.ALL`.
STUDIO_CAPABILITIES = {
    "design.read",
    "design.write",
    "design.build",
    "video.read",
    "video.write",
    "video.build",
    "video.generate",
    "studio.read",
    "studio.write",
}

HOSTS = ("photoshop", "indesign", "premiere")

#: The text styles a frame can be set in, taken from the layout engine's own
#: vocabulary so a specialist cannot invent one InDesign has no style for.
_ELEMENT_STYLES = [
    item.value
    for item in ElementType
    if item not in (ElementType.IMAGE, ElementType.RULE, ElementType.LOGO, ElementType.ADVERTISEMENT)
]


# ---------------------------------------------------------------- board ----


@dataclass
class Artefact:
    """Something a specialist made that another one may need."""

    name: str
    kind: str
    """``design``/``edit``/``file``/``brief``."""
    path: str = ""
    author: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "author": self.author,
            "detail": self.detail,
        }


class Blackboard:
    """What the specialists know, shared across threads.

    The crew runs one specialist per application at the same time, so every
    read and write here is guarded. Nothing else in the studio is shared
    mutable state: a specialist that wants another's work asks the board for
    it, which is also what makes the hand-off auditable.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._designs: dict[str, DesignPlan] = {}
        self._edits: dict[str, EditPlan] = {}
        self._artefacts: dict[str, Artefact] = {}
        self._notes: list[dict[str, str]] = []

    # -- designs ---------------------------------------------------------
    def put_design(self, name: str, plan: DesignPlan) -> DesignPlan:
        """Store or replace a design."""
        with self._lock:
            self._designs[name] = plan
        return plan

    def design(self, name: str) -> DesignPlan:
        """Fetch a design, or explain which ones exist."""
        with self._lock:
            plan = self._designs.get(name)
            if plan is None:
                raise ToolValidationError(
                    f"There is no design called '{name}'",
                    context={"designs": sorted(self._designs)},
                    recovery_action="Call start_design first, or use one of the existing names.",
                )
            return plan

    def designs(self) -> list[str]:
        """Every design name."""
        with self._lock:
            return sorted(self._designs)

    # -- edits -----------------------------------------------------------
    def put_edit(self, name: str, plan: EditPlan) -> EditPlan:
        """Store or replace an edit."""
        with self._lock:
            self._edits[name] = plan
        return plan

    def edit(self, name: str) -> EditPlan:
        """Fetch an edit, or explain which ones exist."""
        with self._lock:
            plan = self._edits.get(name)
            if plan is None:
                raise ToolValidationError(
                    f"There is no edit called '{name}'",
                    context={"edits": sorted(self._edits)},
                    recovery_action="Call start_edit first, or use one of the existing names.",
                )
            return plan

    def edits(self) -> list[str]:
        """Every edit name."""
        with self._lock:
            return sorted(self._edits)

    # -- artefacts -------------------------------------------------------
    def publish(self, artefact: Artefact) -> Artefact:
        """Announce a finished piece of work to the rest of the crew."""
        with self._lock:
            self._artefacts[artefact.name] = artefact
        log.info("%s published %s (%s)", artefact.author or "someone", artefact.name, artefact.kind)
        return artefact

    def artefact(self, name: str) -> Artefact | None:
        """One published artefact."""
        with self._lock:
            return self._artefacts.get(name)

    def artefacts(self, kind: str = "") -> list[Artefact]:
        """Everything published, optionally of one kind."""
        with self._lock:
            items = list(self._artefacts.values())
        return [item for item in items if not kind or item.kind == kind]

    def note(self, author: str, text: str) -> None:
        """Record a decision, so the crew's reasoning survives the run."""
        with self._lock:
            self._notes.append({"author": author, "text": text})

    def notes(self) -> list[dict[str, str]]:
        """Every recorded decision."""
        with self._lock:
            return list(self._notes)

    def summary(self) -> dict[str, Any]:
        """What the board holds, for the art director and the UI."""
        with self._lock:
            return {
                "designs": sorted(self._designs),
                "edits": sorted(self._edits),
                "artefacts": [a.to_dict() for a in self._artefacts.values()],
                "notes": list(self._notes),
            }


# -------------------------------------------------------------- context ----


@dataclass
class StudioContext:
    """Everything the specialists share.

    One per studio run. The Adobe controllers underneath keep their COM
    proxies per thread, and each application is additionally serialised by its
    own lock, so two specialists never drive the same host at once even though
    they run at the same time.
    """

    workspace: Path
    adobe: AdobeService
    formats: FormatRegistry
    board: Blackboard = field(default_factory=Blackboard)
    analyst: ReferenceAnalyst | None = None
    video_provider: Any = None
    bus: EventBus | None = None
    language: str = "fa"
    dpi: int = 300
    style_template: TemplateSpec | None = None
    """The template a one-off document inherits its type scale and colours from."""
    _locks: dict[str, threading.RLock] = field(default_factory=dict, repr=False)
    _lock_guard: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _furniture: Any = field(default=None, repr=False)
    _documents: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def host_lock(self, host: str) -> threading.RLock:
        """The lock that serialises one application.

        Photoshop has one active document, and so does InDesign. Two threads
        scripting the same application would each believe the other's document
        was theirs, so a host is held for the length of a build rather than
        for the length of a call.
        """
        with self._lock_guard:
            lock = self._locks.get(host)
            if lock is None:
                lock = self._locks[host] = threading.RLock()
            return lock

    def path_for(self, name: str, suffix: str) -> Path:
        """A workspace path with a safe file name."""
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "artefact"
        return self.workspace / f"{stem}{suffix}"

    def live(self, host: str) -> bool:
        """Whether an application can actually be driven right now."""
        try:
            app = self.adobe.app(host)
            if not app.installed:
                return False
            return bool(self.adobe.controller(host).available())
        except Exception as exc:  # noqa: BLE001 - probing must never raise
            log.debug("Host %s is not reachable: %s", host, exc)
            return False


# ---------------------------------------------------------------- helpers ---


def _hex(value: str, field_name: str, *, alpha: bool = False) -> str:
    """Validate a colour the way a designer writes one.

    Eight digits carry an alpha channel, which is how a canvas is asked to be
    transparent; it is only accepted where transparency means something.
    """
    text = value.strip()
    if not text.startswith("#"):
        text = f"#{text}"
    body = text[1:]
    if len(body) in (3, 4):
        body = "".join(ch * 2 for ch in body)
    lengths = (6, 8) if alpha else (6,)
    if len(body) not in lengths or any(ch not in "0123456789abcdefABCDEF" for ch in body):
        expected = "#c2410c, or #00000000 for transparent" if alpha else "#c2410c"
        raise ToolValidationError(
            f"'{field_name}' must be a hex colour such as {expected}, not {value!r}"
        )
    return f"#{body.lower()}"


def _json_argument(value: Any, field_name: str) -> dict[str, Any]:
    """Accept an object, or the JSON text a model sometimes sends instead."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(f"'{field_name}' is not valid JSON: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ToolValidationError(f"'{field_name}' must be an object")


def _box(plan: DesignPlan, x: float, y: float, width: float, height: float, units: str) -> Box:
    """A box in whichever units the specialist chose to think in."""
    if units == "mm":
        box = plan.mm(x, y, width, height)
    elif units == "percent":
        canvas = plan.canvas
        box = Box(
            x=canvas.width_px * x / 100.0,
            y=canvas.height_px * y / 100.0,
            width=canvas.width_px * width / 100.0,
            height=canvas.height_px * height / 100.0,
        )
    else:
        box = Box(x=x, y=y, width=width, height=height)
    if box.width <= 0 or box.height <= 0:
        raise ToolValidationError("A layer needs a positive width and height")
    return box


def _layer_report(plan: DesignPlan, layer: Layer) -> dict[str, Any]:
    """What a placement tool tells the specialist it did."""
    return {
        "design": plan.name,
        "layer": layer.name,
        "kind": layer.kind.value,
        "z": layer.z,
        "box_px": layer.box.model_dump(),
        "box_mm": layer.box.to_mm(plan.canvas.dpi),
        "inside_canvas": plan.canvas.box.contains(layer.box, tolerance=1.0),
        "layers": len(plan.layers),
    }


# ------------------------------------------------------- design (Photoshop) --

#: Where a layer sits, named the way a designer names it rather than by
#: number, so an agent can say "put it behind" without computing an order.
#: Each name owns a band of painting orders; a new layer lands at the top of
#: its own band, which keeps the bands from ever crossing each other.
DEPTH_BANDS = {
    "background": (-1000, -501),
    "behind": (-500, -1),
    "normal": (0, 499),
    "front": (500, 899),
    "top": (900, 1999),
}


def build_design_tools(
    context: StudioContext,
    policy: PermissionPolicy | None = None,
    bus: EventBus | None = None,
    *,
    author: str = "photoshop",
) -> ToolRegistry:
    """The Photoshop specialist's tools: a whole design, layer by layer."""
    registry = ToolRegistry(policy or PermissionPolicy(_studio_all()), bus or context.bus)

    registry.register(
        Tool(
            name="start_design",
            description=(
                "Begin a design at a named size. The size may be a preset "
                "('A4', 'instagram post'), or a measurement such as '900x1200 px' "
                "or '70x100 cm'."
            ),
            parameters=[
                Parameter("name", "string", "What to call this design"),
                Parameter("format", "string", "Preset name or explicit size"),
                Parameter(
                    "background", "string",
                    "Canvas colour; #00000000 for a transparent ground, which is what an "
                    "overlay meant to sit over footage needs",
                    required=False, default="#ffffff",
                ),
                Parameter(
                    "mode",
                    "string",
                    "Colour mode; print work is cmyk",
                    required=False,
                    choices=["rgb", "cmyk", "gray", "auto"],
                    default="auto",
                ),
                Parameter("dpi", "integer", "Resolution", required=False, minimum=36, maximum=1200),
            ],
            capability="design.write",
            handler=lambda name, format, background="#ffffff", mode="auto", dpi=None: _start_design(  # noqa: A002
                context, author, name, format, background, mode, dpi
            ),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_text",
            description=(
                "Set type on the design. Give the box in the chosen units and the "
                "typography exactly as it should read: font, size, leading, tracking, "
                "colour and alignment are all honoured."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("name", "string", "Layer name"),
                Parameter("text", "string", "The words themselves"),
                Parameter("x", "number", "Left edge"),
                Parameter("y", "number", "Top edge"),
                Parameter("width", "number", "Box width", minimum=0.01),
                Parameter("height", "number", "Box height", minimum=0.01),
                Parameter(
                    "units", "string", "Units of the box", required=False,
                    choices=["px", "mm", "percent"], default="px",
                ),
                Parameter("font", "string", "Font family", required=False, default=""),
                Parameter("font_style", "string", "Weight or style", required=False, default="Regular"),
                Parameter("size_pt", "number", "Type size in points", required=False, minimum=1, maximum=2000),
                Parameter("leading_pt", "number", "Line spacing in points", required=False, minimum=0),
                Parameter("tracking", "number", "Letter spacing, in 1/1000 em", required=False),
                Parameter("color", "string", "Ink colour", required=False, default="#111111"),
                Parameter(
                    "alignment", "string", "Alignment", required=False,
                    choices=["left", "center", "right", "justify"],
                ),
                Parameter(
                    "direction", "string", "Reading direction", required=False, choices=["ltr", "rtl"],
                ),
                Parameter("all_caps", "boolean", "Set in capitals", required=False),
                Parameter("role", "string", "What it is: headline, kicker, body...", required=False, default=""),
                Parameter(
                    "depth", "string", "Where in the stack", required=False,
                    choices=sorted(DEPTH_BANDS), default="normal",
                ),
                Parameter("opacity", "number", "Layer opacity", required=False, minimum=0, maximum=100),
                Parameter("rotation", "number", "Rotation in degrees", required=False, minimum=-360, maximum=360),
            ],
            capability="design.write",
            handler=lambda **kwargs: _add_text(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_shape",
            description=(
                "Draw a rectangle, rounded rectangle, ellipse or polygon - the panels, "
                "rules and plates a page is built from."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("name", "string", "Layer name"),
                Parameter(
                    "shape", "string", "Which shape", choices=[s.value for s in ShapeKind],
                ),
                Parameter("x", "number", "Left edge"),
                Parameter("y", "number", "Top edge"),
                Parameter("width", "number", "Width", minimum=0.01),
                Parameter("height", "number", "Height", minimum=0.01),
                Parameter(
                    "units", "string", "Units of the box", required=False,
                    choices=["px", "mm", "percent"], default="px",
                ),
                Parameter("color", "string", "Fill colour", required=False, default="#000000"),
                Parameter("radius", "number", "Corner radius in pixels", required=False, minimum=0),
                Parameter("opacity", "number", "Layer opacity", required=False, minimum=0, maximum=100),
                Parameter("rotation", "number", "Rotation in degrees", required=False, minimum=-360, maximum=360),
                Parameter("blend_mode", "string", "Blend mode", required=False, default="normal"),
                Parameter("role", "string", "What it is for", required=False, default=""),
                Parameter(
                    "depth", "string", "Where in the stack", required=False,
                    choices=sorted(DEPTH_BANDS), default="normal",
                ),
                Parameter("points", "array", "Polygon points as [[x,y],...]", required=False),
            ],
            capability="design.write",
            handler=lambda **kwargs: _add_shape(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="place_photo",
            description="Place a picture as a smart object, fitted to its box.",
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("name", "string", "Layer name"),
                Parameter("path", "string", "The image file"),
                Parameter("x", "number", "Left edge"),
                Parameter("y", "number", "Top edge"),
                Parameter("width", "number", "Width", minimum=0.01),
                Parameter("height", "number", "Height", minimum=0.01),
                Parameter(
                    "units", "string", "Units of the box", required=False,
                    choices=["px", "mm", "percent"], default="px",
                ),
                Parameter(
                    "fit", "string", "How it fills the box", required=False,
                    choices=["cover", "contain", "stretch"], default="cover",
                ),
                Parameter("opacity", "number", "Layer opacity", required=False, minimum=0, maximum=100),
                Parameter("blend_mode", "string", "Blend mode", required=False, default="normal"),
                Parameter(
                    "clip_to_below", "boolean",
                    "Clip into the layer beneath, which is how a photo is masked by a shape",
                    required=False,
                ),
                Parameter("role", "string", "What it is for", required=False, default=""),
                Parameter(
                    "depth", "string", "Where in the stack", required=False,
                    choices=sorted(DEPTH_BANDS), default="normal",
                ),
            ],
            capability="design.write",
            handler=lambda **kwargs: _place_photo(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_furniture",
            description=(
                "Draw a piece of newspaper furniture - a tint panel, ruled box, quote "
                "plate, section tab, caption bar, edge arc and the rest - and place it "
                "on the design. This is how the boxes a page is made of get built."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("name", "string", "Layer name"),
                Parameter("kind", "string", "Which piece", choices=[f.value for f in Furniture]),
                Parameter("x_mm", "number", "Left edge in millimetres"),
                Parameter("y_mm", "number", "Top edge in millimetres"),
                Parameter("width_mm", "number", "Width in millimetres", minimum=1),
                Parameter("height_mm", "number", "Height in millimetres", minimum=1),
                Parameter("color", "string", "Panel colour", required=False, default="#f2f0eb"),
                Parameter("accent", "string", "Accent colour", required=False, default="#c2410c"),
                Parameter("ink", "string", "Ink colour", required=False, default="#111111"),
                Parameter("corner_mm", "number", "Corner radius in millimetres", required=False, minimum=0),
                Parameter("rule_pt", "number", "Rule weight in points", required=False, minimum=0),
                Parameter("opacity", "number", "Opacity", required=False, minimum=0, maximum=100),
                Parameter(
                    "depth", "string", "Where in the stack", required=False,
                    choices=sorted(DEPTH_BANDS), default="behind",
                ),
            ],
            capability="design.write",
            handler=lambda **kwargs: _add_furniture(context, author, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="prepare_photo",
            description=(
                "Work a photograph before it goes into a design: cut the subject out of "
                "its background, grade it, crop it to an aspect and convert it for print. "
                "Returns a new file, published for the rest of the crew to place."
            ),
            parameters=[
                Parameter("path", "string", "The photograph, or a published artefact name"),
                Parameter("name", "string", "What to call the result", required=False, default=""),
                Parameter(
                    "cut_out", "boolean",
                    "Remove the background, leaving the subject on transparency",
                    required=False,
                ),
                Parameter(
                    "aspect", "string",
                    "Crop to this aspect, written as 16:9, 4:5 or a number",
                    required=False, default="",
                ),
                Parameter("width_px", "integer", "Resize to this width", required=False, minimum=16, maximum=30000),
                Parameter(
                    "mode", "string", "Colour mode for the result", required=False,
                    choices=["rgb", "cmyk", "gray"],
                ),
                Parameter("brightness", "number", "Brightness, -100 to 100", required=False, minimum=-100, maximum=100),
                Parameter("contrast", "number", "Contrast, -100 to 100", required=False, minimum=-100, maximum=100),
                Parameter("saturation", "number", "Saturation, -100 to 100", required=False, minimum=-100, maximum=100),
                Parameter("sharpen", "number", "Sharpening, 0 to 100", required=False, minimum=0, maximum=100),
                Parameter(
                    "focus_y", "number",
                    "Where the subject sits vertically, 0 at the top and 1 at the foot; a "
                    "portrait crop keeps the head rather than the middle",
                    required=False, minimum=0.0, maximum=1.0,
                ),
            ],
            capability="design.build",
            handler=lambda **kwargs: _prepare_photo(context, author, **kwargs),
            verifier=verify_exists,
        )
    )

    registry.register(
        Tool(
            name="style_layer",
            description=(
                "Give a layer its finish: drop shadow, inner shadow, glow, stroke, "
                "colour overlay or gradient. Each is an object of Photoshop's own "
                "settings, for example {\"color\": \"#000000\", \"opacity\": 40, "
                "\"distance\": 6, \"size\": 12}."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("layer", "string", "Which layer"),
                Parameter("shadow", "object", "Drop shadow", required=False),
                Parameter("inner_shadow", "object", "Inner shadow", required=False),
                Parameter("glow", "object", "Outer glow", required=False),
                Parameter("stroke", "object", "Stroke", required=False),
                Parameter("overlay", "object", "Colour overlay", required=False),
                Parameter("gradient", "object", "Gradient fill", required=False),
            ],
            capability="design.write",
            handler=lambda **kwargs: _style_layer(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="arrange_layer",
            description=(
                "Move, resize, rotate, restack, fade or blend an existing layer. "
                "Only the properties given are changed."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("layer", "string", "Which layer"),
                Parameter("x", "number", "New left edge", required=False),
                Parameter("y", "number", "New top edge", required=False),
                Parameter("width", "number", "New width", required=False, minimum=0.01),
                Parameter("height", "number", "New height", required=False, minimum=0.01),
                Parameter(
                    "units", "string", "Units of the box", required=False,
                    choices=["px", "mm", "percent"], default="px",
                ),
                Parameter("opacity", "number", "Opacity", required=False, minimum=0, maximum=100),
                Parameter("rotation", "number", "Rotation in degrees", required=False, minimum=-360, maximum=360),
                Parameter("blend_mode", "string", "Blend mode", required=False),
                Parameter("z", "integer", "Explicit painting order", required=False),
                Parameter(
                    "depth", "string", "Named depth instead of an order", required=False,
                    choices=sorted(DEPTH_BANDS),
                ),
                Parameter("clip_to_below", "boolean", "Clip into the layer beneath", required=False),
            ],
            capability="design.write",
            handler=lambda **kwargs: _arrange_layer(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="remove_layer",
            description="Take a layer off the design.",
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("layer", "string", "Which layer"),
            ],
            capability="design.write",
            handler=lambda design, layer: _remove_layer(context, design, layer),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="inspect_design",
            description=(
                "Read the design back: the canvas, every layer with its measured box "
                "in both pixels and millimetres, and anything that overlaps or hangs "
                "off the sheet."
            ),
            parameters=[Parameter("design", "string", "Which design")],
            capability="design.read",
            handler=lambda design: _inspect_design(context, design),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="render_design",
            description=(
                "Draw the design to a PNG with the built-in renderer, so it can be "
                "looked at without Photoshop being involved."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter("scale", "number", "Render scale", required=False, minimum=0.05, maximum=2.0),
            ],
            capability="design.read",
            handler=lambda design, scale=None: _render_design(context, author, design, scale),
            verifier=verify_exists,
        )
    )

    registry.register(
        Tool(
            name="check_design",
            description=(
                "Measure the design against what was asked for: sizes that look like a "
                "unit mistake, type too small to read, colours off the brief, layers "
                "outside the sheet."
            ),
            parameters=[Parameter("design", "string", "Which design")],
            capability="design.read",
            handler=lambda design: _check_design(context, design),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="build_in_photoshop",
            description=(
                "Build the design in Photoshop for real and export it. Returns what "
                "Photoshop actually produced, checked layer by layer against the plan; "
                "if Photoshop is not reachable the built-in renderer produces the file "
                "instead and says so."
            ),
            parameters=[
                Parameter("design", "string", "Which design"),
                Parameter(
                    "export_as", "string", "File extension", required=False,
                    choices=["png", "jpg", "psd", "tif", "pdf"], default="png",
                ),
                Parameter("quality", "integer", "JPEG quality", required=False, minimum=1, maximum=100),
                Parameter("flatten", "boolean", "Flatten on export", required=False),
            ],
            capability="design.build",
            handler=lambda design, export_as="png", quality=None, flatten=None: _build_in_photoshop(
                context, author, design, export_as, quality, flatten
            ),
            verifier=verify_exists,
        )
    )

    registry.register(
        Tool(
            name="save_design",
            description="Write the design out as JSON so it can be reopened or reused.",
            parameters=[Parameter("design", "string", "Which design")],
            capability="design.write",
            handler=lambda design: _save_design(context, author, design),
            verifier=verify_exists,
        )
    )

    _register_shared(registry, context, author)
    return registry


def _studio_all() -> set[str]:
    """Every capability a studio specialist could hold."""
    return set(PermissionPolicy.ALL) | STUDIO_CAPABILITIES


def _start_design(
    context: StudioContext,
    author: str,
    name: str,
    request: str,
    background: str,
    mode: str,
    dpi: int | None,
) -> dict[str, Any]:
    item = context.formats.resolve(request, dpi=dpi or None)
    plan = DesignPlan.for_format(
        item,
        name=name,
        language=context.language,
        background=_hex(background, "background", alpha=True),
        mode=None if mode == "auto" else mode,  # type: ignore[arg-type]
    )
    plan.brief = request
    context.board.put_design(name, plan)
    context.board.note(author, f"Started '{name}' at {item.describe()}")
    return {
        "design": name,
        "format": item.name,
        "width_px": plan.canvas.width_px,
        "height_px": plan.canvas.height_px,
        "width_mm": round(item.width_mm, 1),
        "height_mm": round(item.height_mm, 1),
        "dpi": plan.canvas.dpi,
        "mode": plan.canvas.mode,
        "aspect": item.aspect_label(),
        "medium": item.medium.value,
        "safe_box": plan.canvas.safe_box.model_dump() if plan.canvas.safe_box else None,
    }


def _depth(plan: DesignPlan, depth: str) -> int:
    """Turn a named depth into a painting order the plan will keep.

    Within a band a new layer goes on top of the ones already there, and a
    full band stops at its ceiling rather than leaking into the next one.
    """
    low, high = DEPTH_BANDS.get(depth or "normal", DEPTH_BANDS["normal"])
    used = [layer.z for layer in plan.layers if low <= layer.z <= high]
    return min(high, (max(used) if used else low - 10) + 10)


def _add_text(
    context: StudioContext,
    design: str,
    name: str,
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
    units: str = "px",
    font: str = "",
    font_style: str = "Regular",
    size_pt: float | None = None,
    leading_pt: float | None = None,
    tracking: float | None = None,
    color: str = "#111111",
    alignment: str | None = None,
    direction: str | None = None,
    all_caps: bool | None = None,
    role: str = "",
    depth: str = "normal",
    opacity: float | None = None,
    rotation: float | None = None,
) -> dict[str, Any]:
    plan = context.board.design(design)
    box = _box(plan, x, y, width, height, units)
    rtl = (direction or ("rtl" if context.language in ("fa", "ar", "he", "ur") else "ltr")) == "rtl"
    layer = Layer(
        name=name,
        kind=LayerKind.TEXT,
        box=box,
        text=text,
        font=font,
        font_style=font_style,
        size_pt=float(size_pt) if size_pt else _default_size(box, plan.canvas.dpi),
        leading_pt=float(leading_pt or 0.0),
        tracking=float(tracking or 0.0),
        color=_hex(color, "color"),
        alignment=alignment or ("right" if rtl else "left"),  # type: ignore[arg-type]
        direction="rtl" if rtl else "ltr",
        all_caps=bool(all_caps),
        role=role,
        opacity=100.0 if opacity is None else float(opacity),
        rotation=float(rotation or 0.0),
        z=_depth(plan, depth),
    )
    if size_pt:
        # An explicit size is honoured, whatever it does: it was asked for.
        # What is not silent is the consequence, which comes back in the
        # report so the mistake is corrected rather than printed.
        layer.size_pt = float(size_pt)
    else:
        fit_to_box(plan, layer, start_pt=_default_size(box, plan.canvas.dpi) * 1.6)
    _replace(plan, layer)
    measured = measure_text(plan, layer)
    report = _layer_report(plan, layer)
    report.update(
        {
            "size_pt": layer.size_pt,
            "leading_pt": layer.leading_pt,
            "alignment": layer.alignment,
            "direction": layer.direction,
            "lines": measured["lines"],
            "overflow": measured["overflow"],
            "fits": measured["overflow"] <= 0.0,
        }
    )
    if measured["overflow"] > 0.0:
        report["warning"] = (
            f"At {layer.size_pt:g} pt this copy needs {measured['measured_height']:.0f} px of a "
            f"{layer.box.height:.0f} px box and will run out of it. Leave the size out to have "
            "it fitted, or make the box taller."
        )
    return report


def _default_size(box: Box, dpi: int) -> float:
    """A readable size for a box whose type size was not given.

    Two thirds of the box height for a single line, capped so a tall box does
    not produce absurd type. Better than a silent zero, which is what the
    model returns when it forgets the argument.
    """
    points = box.height / max(1, dpi) * 72.0
    return round(min(max(points * 0.62, 6.0), 400.0), 1)


def _replace(plan: DesignPlan, layer: Layer) -> Layer:
    """Add a layer, replacing an existing one of the same name.

    The painting order is reapplied after the plan has taken the layer:
    :meth:`DesignPlan.add` treats a falsy ``z`` as "not set" and restacks the
    layer on top, and the front of the ``normal`` band is legitimately zero.
    """
    wanted = layer.z
    existing = plan.layer(layer.name)
    if existing is not None:
        plan.layers.remove(existing)
    plan.add(layer)
    layer.z = wanted
    return layer


def _add_shape(
    context: StudioContext,
    design: str,
    name: str,
    shape: str,
    x: float,
    y: float,
    width: float,
    height: float,
    units: str = "px",
    color: str = "#000000",
    radius: float | None = None,
    opacity: float | None = None,
    rotation: float | None = None,
    blend_mode: str = "normal",
    role: str = "",
    depth: str = "normal",
    points: list[Any] | None = None,
) -> dict[str, Any]:
    plan = context.board.design(design)
    box = _box(plan, x, y, width, height, units)
    kind = ShapeKind(shape)
    if kind is ShapeKind.POLYGON and not points:
        raise ToolValidationError("A polygon needs its 'points'")
    layer = Layer(
        name=name,
        kind=LayerKind.SHAPE,
        box=box,
        shape=kind,
        radius=float(radius or 0.0),
        color=_hex(color, "color"),
        opacity=100.0 if opacity is None else float(opacity),
        rotation=float(rotation or 0.0),
        blend_mode=blend_mode,
        role=role,
        points=[(float(p[0]), float(p[1])) for p in (points or []) if len(p) >= 2],
        z=_depth(plan, depth),
    )
    _replace(plan, layer)
    return _layer_report(plan, layer)


def _place_photo(
    context: StudioContext,
    design: str,
    name: str,
    path: str,
    x: float,
    y: float,
    width: float,
    height: float,
    units: str = "px",
    fit: str = "cover",
    opacity: float | None = None,
    blend_mode: str = "normal",
    clip_to_below: bool | None = None,
    role: str = "",
    depth: str = "normal",
) -> dict[str, Any]:
    plan = context.board.design(design)
    source = Path(path)
    if not source.exists():
        published = context.board.artefact(path)
        if published and published.path and Path(published.path).exists():
            source = Path(published.path)
        else:
            raise ToolValidationError(
                f"The image {path} does not exist",
                context={"published": [a.name for a in context.board.artefacts("file")]},
                recovery_action="Use a file that exists, or the name of a published artefact.",
            )
    layer = Layer(
        name=name,
        kind=LayerKind.IMAGE,
        box=_box(plan, x, y, width, height, units),
        path=str(source),
        fit=fit,  # type: ignore[arg-type]
        opacity=100.0 if opacity is None else float(opacity),
        blend_mode=blend_mode,
        clip_to_below=bool(clip_to_below),
        role=role,
        z=_depth(plan, depth),
    )
    _replace(plan, layer)
    report = _layer_report(plan, layer)
    report["path"] = str(source)
    return report


def _add_furniture(
    context: StudioContext,
    author: str,
    design: str,
    name: str,
    kind: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    color: str = "#f2f0eb",
    accent: str = "#c2410c",
    ink: str = "#111111",
    corner_mm: float | None = None,
    rule_pt: float | None = None,
    opacity: float | None = None,
    depth: str = "behind",
) -> dict[str, Any]:
    plan = context.board.design(design)
    spec = FurnitureSpec(
        kind=Furniture(kind),
        width_mm=float(width_mm),
        height_mm=float(height_mm),
        dpi=plan.canvas.dpi,
        color=_hex(color, "color"),
        accent=_hex(accent, "accent"),
        ink=_hex(ink, "ink"),
        corner_mm=float(corner_mm or 0.0),
        rule_pt=float(rule_pt) if rule_pt is not None else 0.75,
        opacity=100.0 if opacity is None else float(opacity),
        direction="rtl" if context.language in ("fa", "ar", "he", "ur") else "ltr",
    )
    factory = _furniture_factory(context)
    with context.host_lock("photoshop"):
        target = factory.make(spec)
    placed = factory.placement(spec, Box(x=x_mm, y=y_mm, width=width_mm, height=height_mm), plan.canvas.dpi)
    layer = Layer(
        name=name,
        kind=LayerKind.IMAGE,
        box=plan.mm(placed.x, placed.y, placed.width, placed.height),
        path=str(target),
        fit="stretch",
        role=f"furniture:{kind}",
        z=_depth(plan, depth),
    )
    _replace(plan, layer)
    context.board.publish(
        Artefact(name=f"{design}:{name}", kind="file", path=str(target), author=author, detail={"furniture": kind})
    )
    report = _layer_report(plan, layer)
    report.update({"furniture": kind, "path": str(target)})
    return report


def _furniture_factory(context: StudioContext) -> FurnitureFactory:
    """The factory for this run, made once and reused for its cache.

    Made under the context's own guard: two specialists asking for their first
    piece at the same moment would otherwise get a cache each, and the same
    panel would be drawn twice.
    """
    with context._lock_guard:  # noqa: SLF001 - the context's own guard
        if context._furniture is None:  # noqa: SLF001
            photoshop = context.adobe.photoshop if context.live("photoshop") else None
            context._furniture = FurnitureFactory(  # noqa: SLF001
                context.workspace / "furniture", photoshop=photoshop
            )
        return context._furniture  # noqa: SLF001


def _prepare_photo(
    context: StudioContext,
    author: str,
    path: str,
    name: str = "",
    cut_out: bool | None = None,
    aspect: str = "",
    width_px: int | None = None,
    mode: str | None = None,
    brightness: float | None = None,
    contrast: float | None = None,
    saturation: float | None = None,
    sharpen: float | None = None,
    focus_y: float | None = None,
) -> dict[str, Any]:
    """Run the photograph through Photoshop, or through the local engine.

    The same call either way: what changes is the quality of the cut-out and
    the grading, not whether the design gets its picture.
    """
    source = _resolve_media(context, path)
    stem = _slug(name or f"{source.stem}_prepared")
    directory = context.workspace / "photos"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{stem}{'.png' if cut_out else source.suffix or '.jpg'}"

    adjust = {
        key: float(value)
        for key, value in (
            ("brightness", brightness),
            ("contrast", contrast),
            ("saturation", saturation),
            ("sharpen", sharpen),
        )
        if value is not None
    }
    with context.host_lock("photoshop"):
        result = context.adobe.photoshop.process_image(
            source,
            target,
            aspect=_aspect(aspect) if aspect else None,
            width_px=int(width_px) if width_px else None,
            dpi=context.dpi,
            mode=mode,  # type: ignore[arg-type]
            remove_background=bool(cut_out),
            adjust=adjust or None,
            focus_y=0.42 if focus_y is None else float(focus_y),
        )
    produced = Path(result.get("path") or target)
    context.board.publish(
        Artefact(
            name=stem,
            kind="file",
            path=str(produced),
            author=author,
            detail={"engine": result.get("engine"), "from": str(source), "cut_out": bool(cut_out)},
        )
    )
    return {
        "name": stem,
        "path": str(produced),
        "engine": result.get("engine", "unknown"),
        "cut_out": bool(cut_out),
        "width": result.get("width"),
        "height": result.get("height"),
        # Whatever the engine could not do, in its own words - a cut-out it
        # could not find an edge for, a CMYK conversion that needs Photoshop.
        "notes": result.get("notes") or [],
    }


def _aspect(text: str) -> float:
    """Read an aspect written the way designers write one."""
    cleaned = text.strip().replace("x", ":").replace("/", ":")
    if ":" in cleaned:
        across, _, down = cleaned.partition(":")
        try:
            width, height = float(across), float(down)
        except ValueError:
            raise ToolValidationError(f"'{text}' is not an aspect such as 16:9 or 4:5") from None
        if height <= 0 or width <= 0:
            raise ToolValidationError(f"'{text}' is not an aspect this studio can use")
        return width / height
    try:
        value = float(cleaned)
    except ValueError:
        raise ToolValidationError(f"'{text}' is not an aspect such as 16:9 or 4:5") from None
    if value <= 0:
        raise ToolValidationError(f"'{text}' is not an aspect this studio can use")
    return value


def _style_layer(context: StudioContext, design: str, layer: str, **effects: Any) -> dict[str, Any]:
    plan = context.board.design(design)
    target = plan.layer(layer)
    if target is None:
        raise ToolValidationError(
            f"'{design}' has no layer called '{layer}'",
            context={"layers": [item.name for item in plan.layers]},
        )
    given = {key: _json_argument(value, key) for key, value in effects.items() if value not in (None, {}, "")}
    if not given:
        raise ToolValidationError("style_layer was given no effect to apply")
    merged = target.effects.model_dump()
    merged.update(given)
    target.effects = Effects(**merged)
    return {
        "design": design,
        "layer": layer,
        "effects": [key for key, value in target.effects.model_dump().items() if value],
    }


def _arrange_layer(
    context: StudioContext,
    design: str,
    layer: str,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    units: str = "px",
    opacity: float | None = None,
    rotation: float | None = None,
    blend_mode: str | None = None,
    z: int | None = None,
    depth: str | None = None,
    clip_to_below: bool | None = None,
) -> dict[str, Any]:
    plan = context.board.design(design)
    target = plan.layer(layer)
    if target is None:
        raise ToolValidationError(
            f"'{design}' has no layer called '{layer}'",
            context={"layers": [item.name for item in plan.layers]},
        )
    if target.locked:
        raise ToolValidationError(f"'{layer}' is locked")
    if any(value is not None for value in (x, y, width, height)):
        current = target.box.to_mm(plan.canvas.dpi) if units == "mm" else target.box.model_dump()
        if units == "percent":
            canvas = plan.canvas
            current = {
                "x": target.box.x / canvas.width_px * 100.0,
                "y": target.box.y / canvas.height_px * 100.0,
                "width": target.box.width / canvas.width_px * 100.0,
                "height": target.box.height / canvas.height_px * 100.0,
            }
        target.box = _box(
            plan,
            current["x"] if x is None else x,
            current["y"] if y is None else y,
            current["width"] if width is None else width,
            current["height"] if height is None else height,
            units,
        )
    if opacity is not None:
        target.opacity = float(opacity)
    if rotation is not None:
        target.rotation = float(rotation)
    if blend_mode:
        target.blend_mode = blend_mode
    if clip_to_below is not None:
        target.clip_to_below = bool(clip_to_below)
    if z is not None:
        target.z = int(z)
    elif depth:
        target.z = _depth(plan, depth)
    return _layer_report(plan, target)


def _remove_layer(context: StudioContext, design: str, layer: str) -> dict[str, Any]:
    plan = context.board.design(design)
    target = plan.layer(layer)
    if target is None:
        raise ToolValidationError(f"'{design}' has no layer called '{layer}'")
    plan.layers.remove(target)
    return {"design": design, "removed": layer, "layers": len(plan.layers)}


def _inspect_design(context: StudioContext, design: str) -> dict[str, Any]:
    plan = context.board.design(design)
    canvas = plan.canvas.box
    layers = []
    for layer in plan.ordered():
        entry = {
            "name": layer.name,
            "kind": layer.kind.value,
            "role": layer.role,
            "z": layer.z,
            "opacity": layer.opacity,
            "blend_mode": layer.blend_mode,
            "box_px": {k: round(v, 1) for k, v in layer.box.model_dump().items()},
            "box_mm": layer.box.to_mm(plan.canvas.dpi),
            "outside_canvas": not canvas.contains(layer.box, tolerance=1.0),
            "effects": [key for key, value in layer.effects.model_dump().items() if value],
        }
        if layer.is_text:
            entry.update(
                {
                    "text": layer.text[:120],
                    "font": layer.font,
                    "size_pt": layer.size_pt,
                    "leading_pt": layer.leading_pt or round(layer.size_pt * 1.2, 2),
                    "tracking": layer.tracking,
                    "color": layer.color,
                    "alignment": layer.alignment,
                    "direction": layer.direction,
                }
            )
        elif layer.is_image:
            entry.update({"path": layer.path, "fit": layer.fit, "clip_to_below": layer.clip_to_below})
        elif layer.kind is LayerKind.SHAPE:
            entry.update({"shape": layer.shape.value, "color": layer.color, "radius": layer.radius})
        layers.append(entry)

    collisions = []
    text_and_pictures = [
        layer for layer in plan.layers if layer.is_text or (layer.is_image and not layer.clip_to_below)
    ]
    for index, first in enumerate(text_and_pictures):
        for second in text_and_pictures[index + 1 :]:
            shared = first.box.overlaps(second.box)
            smaller = min(first.box.area, second.box.area) or 1.0
            if shared / smaller > 0.35:
                collisions.append(
                    {
                        "layers": [first.name, second.name],
                        "share": round(shared / smaller, 2),
                    }
                )
    return {
        "design": design,
        "canvas": {
            "width_px": plan.canvas.width_px,
            "height_px": plan.canvas.height_px,
            "dpi": plan.canvas.dpi,
            "mode": plan.canvas.mode,
            "background": plan.canvas.background,
            "format": plan.canvas.format_name,
        },
        "safe_box": plan.canvas.safe_box.model_dump() if plan.canvas.safe_box else None,
        "layers": layers,
        "collisions": collisions,
        "empty": not plan.layers,
    }


def _render_design(context: StudioContext, author: str, design: str, scale: float | None) -> dict[str, Any]:
    plan = context.board.design(design)
    if not plan.layers:
        raise ToolValidationError(f"'{design}' has no layers to render yet")
    target = context.path_for(f"{design}_preview", ".png")
    factor = float(scale) if scale else _preview_scale(plan)
    DesignRenderer(plan, scale=factor).render_to(target)
    context.board.publish(
        Artefact(name=f"{design}:preview", kind="file", path=str(target), author=author,
                 detail={"scale": factor})
    )
    return {"design": design, "path": str(target), "scale": round(factor, 3)}


def _preview_scale(plan: DesignPlan) -> float:
    """Keep a preview under a couple of thousand pixels on its long edge."""
    longest = max(plan.canvas.width_px, plan.canvas.height_px)
    return round(min(1.0, 2000.0 / max(1, longest)), 3)


def _check_design(context: StudioContext, design: str) -> dict[str, Any]:
    plan = context.board.design(design)
    report = DesignFidelity(plan).check_plan()
    out = report.to_dict()
    out["design"] = design
    return out


def _build_in_photoshop(
    context: StudioContext,
    author: str,
    design: str,
    export_as: str,
    quality: int | None,
    flatten: bool | None,
) -> dict[str, Any]:
    plan = context.board.design(design)
    if not plan.layers:
        raise ToolValidationError(f"'{design}' has no layers to build yet")
    target = context.path_for(design, f".{export_as}")

    if not context.live("photoshop"):
        DesignRenderer(plan).render_to(target)
        context.board.publish(
            Artefact(name=design, kind="file", path=str(target), author=author, detail={"engine": "builtin"})
        )
        return {
            "design": design,
            "path": str(target),
            "engine": "builtin",
            "note": (
                "Photoshop is not reachable on this machine, so the built-in renderer "
                "produced the file. The design plan is unchanged and will build in "
                "Photoshop as soon as it is available."
            ),
            "fidelity": DesignFidelity(plan).check_plan().to_dict(),
        }

    with context.host_lock("photoshop"):
        context.adobe.photoshop.connect()
        result = context.adobe.photoshop.build_design(
            plan,
            export=target,
            quality=92 if quality is None else int(quality),
            flatten=True if flatten is None else bool(flatten),
        )
        report = context.adobe.photoshop.design_report()

    fidelity = DesignFidelity(plan).check(report, host="photoshop")
    context.board.publish(
        Artefact(
            name=design,
            kind="file",
            path=str(target),
            author=author,
            detail={"engine": "photoshop", "faithful": fidelity.faithful},
        )
    )
    return {
        "design": design,
        "path": str(target),
        "engine": "photoshop",
        "built": len(result.get("layers") or []),
        "failed": result.get("failed") or [],
        "fidelity": fidelity.to_dict(),
    }


def _save_design(context: StudioContext, author: str, design: str) -> dict[str, Any]:
    plan = context.board.design(design)
    target = plan.save(context.path_for(f"{design}_design", ".json"))
    context.board.publish(
        Artefact(name=f"{design}:plan", kind="design", path=str(target), author=author)
    )
    return {"design": design, "path": str(target), "layers": len(plan.layers)}


# --------------------------------------------------------- video (Premiere) --


def build_video_tools(
    context: StudioContext,
    policy: PermissionPolicy | None = None,
    bus: EventBus | None = None,
    *,
    author: str = "premiere",
) -> ToolRegistry:
    """The Premiere specialist's tools: a whole edit, step by step."""
    registry = ToolRegistry(policy or PermissionPolicy(_studio_all()), bus or context.bus)

    registry.register(
        Tool(
            name="start_edit",
            description=(
                "Begin an edit at a named size - 'instagram reel', '1920x1080 px', "
                "'youtube' - which sets the sequence, its frame rate and the safe area "
                "the platform's own interface covers."
            ),
            parameters=[
                Parameter("name", "string", "What to call this edit"),
                Parameter("format", "string", "Preset name or explicit size"),
                Parameter("fps", "number", "Frames per second", required=False, minimum=1, maximum=240),
                Parameter("video_tracks", "integer", "How many video tracks", required=False, minimum=1, maximum=20),
                Parameter("audio_tracks", "integer", "How many audio tracks", required=False, minimum=1, maximum=20),
            ],
            capability="video.write",
            handler=lambda name, format, fps=None, video_tracks=None, audio_tracks=None: _start_edit(  # noqa: A002
                context, author, name, format, fps, video_tracks, audio_tracks
            ),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="import_footage",
            description="Register footage so the edit can use it. Paths must exist.",
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("paths", "array", "Files to import"),
                Parameter("bin", "string", "Bin name", required=False, default="Footage"),
            ],
            capability="video.write",
            handler=lambda edit, paths, bin="Footage": _import_footage(context, edit, paths, bin),  # noqa: A002
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_clip",
            description=(
                "Put a piece of footage on the timeline. Leave 'at' out to append it "
                "after whatever is already on that track."
            ),
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("item", "string", "Footage name or path"),
                Parameter("track", "integer", "Video track", required=False, minimum=0, maximum=19),
                Parameter("at", "number", "Start, in seconds", required=False, minimum=0),
                Parameter("in_point", "number", "Trim in, in seconds", required=False, minimum=0),
                Parameter("out_point", "number", "Trim out, in seconds", required=False, minimum=0),
                Parameter("note", "string", "Why this shot is here", required=False, default=""),
            ],
            capability="video.write",
            handler=lambda **kwargs: _add_clip(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_transition",
            description="Put a transition on a cut. 'after_clip' is the index of the clip it follows.",
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("transition", "string", "Transition name, e.g. 'Cross Dissolve'"),
                Parameter("after_clip", "integer", "Index of the clip it follows", minimum=0, maximum=999),
                Parameter("duration", "number", "Length in seconds", required=False, minimum=0.04, maximum=30),
                Parameter("track", "integer", "Video track", required=False, minimum=0, maximum=19),
            ],
            capability="video.write",
            handler=lambda **kwargs: _add_transition(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="apply_video_effect",
            description=(
                "Apply an effect to a clip and set its parameters, for example "
                "{'Blurriness': 12}. Use list_video_effects first when unsure of the name."
            ),
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("clip", "integer", "Index of the clip", minimum=0, maximum=999),
                Parameter("effect", "string", "Effect name"),
                Parameter("parameters", "object", "Parameter values", required=False),
                Parameter("track", "integer", "Video track", required=False, minimum=0, maximum=19),
            ],
            capability="video.write",
            handler=lambda **kwargs: _apply_effect(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="transform_clip",
            description="Scale, move, rotate or fade a clip - the motion a still needs to feel alive.",
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("clip", "integer", "Index of the clip", minimum=0, maximum=999),
                Parameter("scale", "number", "Scale, as a percentage", required=False, minimum=1, maximum=1000),
                Parameter("rotation", "number", "Rotation in degrees", required=False, minimum=-360, maximum=360),
                Parameter("opacity", "number", "Opacity percentage", required=False, minimum=0, maximum=100),
                Parameter("position", "array", "Position as [x, y] in pixels", required=False),
                Parameter("at", "number", "Keyframe time, in seconds", required=False, minimum=0),
                Parameter("track", "integer", "Video track", required=False, minimum=0, maximum=19),
            ],
            capability="video.write",
            handler=lambda **kwargs: _transform_clip(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_overlay",
            description=(
                "Composite a still over the footage - this is how a Photoshop design, a "
                "title card or a lower third gets onto the picture. Pass a file path or "
                "the name of a design another specialist published."
            ),
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("path", "string", "Image file, or a published artefact name"),
                Parameter("at", "number", "Start, in seconds", required=False, minimum=0),
                Parameter("duration", "number", "Length in seconds", required=False, minimum=0.04, maximum=3600),
                Parameter("fade", "number", "Fade in and out, in seconds", required=False, minimum=0, maximum=10),
                Parameter("track", "integer", "Video track it sits on", required=False, minimum=1, maximum=19),
            ],
            capability="video.write",
            handler=lambda **kwargs: _add_overlay(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_audio",
            description="Lay a sound on an audio track.",
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("item", "string", "Audio file or imported name"),
                Parameter("track", "integer", "Audio track", required=False, minimum=0, maximum=19),
                Parameter("at", "number", "Start, in seconds", required=False, minimum=0),
                Parameter("duration", "number", "Length in seconds", required=False, minimum=0.04),
            ],
            capability="video.write",
            handler=lambda **kwargs: _add_audio(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="set_clip_speed",
            description="Change a clip's playback rate; 200 is twice as fast, 50 is half.",
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("clip_name", "string", "Name of the clip on the timeline"),
                Parameter("factor", "number", "Speed as a percentage", minimum=1, maximum=10000),
                Parameter("track", "integer", "Video track", required=False, minimum=0, maximum=19),
            ],
            capability="video.write",
            handler=lambda **kwargs: _set_speed(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="inspect_edit",
            description="Read the edit back: the sequence, its footage and every step in order.",
            parameters=[Parameter("edit", "string", "Which edit")],
            capability="video.read",
            handler=lambda edit: _inspect_edit(context, edit),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="list_video_effects",
            description=(
                "The effects this Premiere installation actually has. Only meaningful "
                "when Premiere is reachable."
            ),
            parameters=[
                Parameter("contains", "string", "Filter by name", required=False, default=""),
            ],
            capability="video.read",
            handler=lambda contains="": _list_effects(context, contains),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="build_in_premiere",
            description=(
                "Build the edit in Premiere for real, through the extension panel, and "
                "report what the timeline actually contains."
            ),
            parameters=[Parameter("edit", "string", "Which edit")],
            capability="video.build",
            handler=lambda edit: _build_in_premiere(context, author, edit),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="export_video",
            description="Render the sequence through Media Encoder.",
            parameters=[
                Parameter("edit", "string", "Which edit"),
                Parameter("preset", "string", "Path to an .epr preset", required=False, default=""),
                Parameter("queue", "boolean", "Queue rather than render immediately", required=False),
            ],
            capability="video.build",
            handler=lambda edit, preset="", queue=None: _export_video(context, author, edit, preset, queue),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="generate_video",
            description=(
                "Ask the configured generative video service for a shot. Returns the "
                "file, which can then be imported as footage."
            ),
            parameters=[
                Parameter("prompt", "string", "What the shot shows"),
                Parameter("seconds", "number", "How long", required=False, minimum=1, maximum=60),
                Parameter("aspect_ratio", "string", "Aspect, e.g. 9:16", required=False, default="16:9"),
                Parameter("image_path", "string", "A still to animate", required=False, default=""),
                Parameter("motion", "string", "How the camera moves", required=False, default=""),
            ],
            capability="video.generate",
            handler=lambda **kwargs: _generate_video(context, author, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="save_edit",
            description="Write the edit out as JSON so it can be reopened or reused.",
            parameters=[Parameter("edit", "string", "Which edit")],
            capability="video.write",
            handler=lambda edit: _save_edit(context, author, edit),
            verifier=verify_exists,
        )
    )

    _register_shared(registry, context, author)
    return registry


def _start_edit(
    context: StudioContext,
    author: str,
    name: str,
    request: str,
    fps: float | None,
    video_tracks: int | None,
    audio_tracks: int | None,
) -> dict[str, Any]:
    item = context.formats.resolve(request)
    plan = EditPlan.for_format(item, name=name, language=context.language)
    if fps:
        plan.sequence.fps = float(fps)
    if video_tracks:
        plan.sequence.video_tracks = int(video_tracks)
    if audio_tracks:
        plan.sequence.audio_tracks = int(audio_tracks)
    plan.brief = request
    context.board.put_edit(name, plan)
    context.board.note(author, f"Started the edit '{name}' at {item.describe()}")
    return {
        "edit": name,
        "format": item.name,
        "width": plan.sequence.width,
        "height": plan.sequence.height,
        "fps": plan.sequence.fps,
        "aspect": item.aspect_label(),
        "video_tracks": plan.sequence.video_tracks,
        "audio_tracks": plan.sequence.audio_tracks,
        "safe_area": plan.safe_area,
    }


def _resolve_media(context: StudioContext, reference: str) -> Path:
    """A file path, whether the specialist named a file or an artefact."""
    path = Path(reference)
    if path.exists():
        return path
    published = context.board.artefact(reference)
    if published and published.path and Path(published.path).exists():
        return Path(published.path)
    raise ToolValidationError(
        f"'{reference}' is neither a file that exists nor a published artefact",
        context={"published": [a.name for a in context.board.artefacts("file")]},
        recovery_action="Import the file first, or use the name another specialist published.",
    )


def _import_footage(context: StudioContext, edit: str, paths: list[Any], bin_name: str) -> dict[str, Any]:
    plan = context.board.edit(edit)
    resolved = [_resolve_media(context, str(item)) for item in paths]
    plan.bin = bin_name or plan.bin
    for path in resolved:
        if str(path) not in plan.footage:
            plan.footage.append(str(path))
    return {
        "edit": edit,
        "imported": [path.name for path in resolved],
        "footage": len(plan.footage),
        "bin": plan.bin,
    }


def _clip_count(plan: EditPlan, track: int) -> int:
    """How many clips are already on a track - the index the next one takes."""
    return sum(1 for step in plan.steps if step.action is StepKind.CLIP and step.track == track)


def _add_clip(
    context: StudioContext,
    edit: str,
    item: str,
    track: int | None = None,
    at: float | None = None,
    in_point: float | None = None,
    out_point: float | None = None,
    note: str = "",
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    lane = int(track or 0)
    if lane >= plan.sequence.video_tracks:
        raise ToolValidationError(
            f"The sequence has {plan.sequence.video_tracks} video track(s); track {lane} is not one of them"
        )
    if in_point is not None and out_point is not None and out_point <= in_point:
        raise ToolValidationError("'out_point' must come after 'in_point'")
    reference = item
    if Path(item).suffix:
        reference = str(_resolve_media(context, item))
    index = _clip_count(plan, lane)
    step = plan.add_clip(
        reference, track=lane, at=at, in_point=in_point, out_point=out_point, note=note
    )
    return {
        "edit": edit,
        "clip": index,
        "item": step.item,
        "track": lane,
        "at": step.at,
        "duration": plan.duration,
        "steps": len(plan.steps),
    }


def _require_clip(plan: EditPlan, track: int, index: int, what: str) -> None:
    """Refuse a step that names a clip which is not on the timeline."""
    count = _clip_count(plan, track)
    if count == 0:
        raise ToolValidationError(
            f"Track {track} has no clips yet, so there is nothing for the {what} to act on"
        )
    if index >= count:
        raise ToolValidationError(
            f"Track {track} has {count} clip(s); clip {index} is not one of them",
            context={"clips": count},
        )


def _add_transition(
    context: StudioContext,
    edit: str,
    transition: str,
    after_clip: int,
    duration: float | None = None,
    track: int | None = None,
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    lane = int(track or 0)
    _require_clip(plan, lane, int(after_clip), "transition")
    if _clip_count(plan, lane) < int(after_clip) + 2:
        raise ToolValidationError(
            f"A transition after clip {after_clip} needs a clip on either side of the cut; "
            f"track {lane} has {_clip_count(plan, lane)}"
        )
    plan.add(
        Step(
            action=StepKind.TRANSITION,
            transition=transition,
            after_clip=int(after_clip),
            duration=duration,
            track=lane,
        )
    )
    return {"edit": edit, "transition": transition, "after_clip": int(after_clip), "track": lane}


def _apply_effect(
    context: StudioContext,
    edit: str,
    clip: int,
    effect: str,
    parameters: dict[str, Any] | None = None,
    track: int | None = None,
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    lane = int(track or 0)
    _require_clip(plan, lane, int(clip), "effect")
    values = _json_argument(parameters, "parameters") if parameters else {}
    plan.add(
        Step(action=StepKind.EFFECT, effect=effect, clip=int(clip), parameters=values, track=lane)
    )
    return {"edit": edit, "effect": effect, "clip": int(clip), "parameters": values, "track": lane}


def _transform_clip(
    context: StudioContext,
    edit: str,
    clip: int,
    scale: float | None = None,
    rotation: float | None = None,
    opacity: float | None = None,
    position: list[Any] | None = None,
    at: float | None = None,
    track: int | None = None,
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    lane = int(track or 0)
    _require_clip(plan, lane, int(clip), "transform")
    if all(value is None for value in (scale, rotation, opacity, position)):
        raise ToolValidationError("transform_clip was given nothing to change")
    point = [float(position[0]), float(position[1])] if position and len(position) >= 2 else None
    plan.add(
        Step(
            action=StepKind.TRANSFORM,
            clip=int(clip),
            track=lane,
            at=at,
            scale=scale,
            rotation=rotation,
            opacity=opacity,
            position=point,
        )
    )
    return {
        "edit": edit,
        "clip": int(clip),
        "track": lane,
        "scale": scale,
        "rotation": rotation,
        "opacity": opacity,
        "position": point,
        "keyframed": at is not None,
    }


def _add_overlay(
    context: StudioContext,
    edit: str,
    path: str,
    at: float | None = None,
    duration: float | None = None,
    fade: float | None = None,
    track: int | None = None,
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    lane = int(track if track is not None else 1)
    if lane >= plan.sequence.video_tracks:
        raise ToolValidationError(
            f"The sequence has {plan.sequence.video_tracks} video track(s); "
            f"an overlay cannot sit on track {lane}"
        )
    if lane == 0:
        raise ToolValidationError(
            "An overlay belongs above the footage; use track 1 or higher"
        )
    source = _resolve_media(context, path)
    plan.add(
        Step(
            action=StepKind.OVERLAY,
            path=str(source),
            at=at,
            duration=duration,
            fade=fade,
            track=lane,
            bin=plan.bin,
        )
    )
    return {
        "edit": edit,
        "overlay": source.name,
        "path": str(source),
        "track": lane,
        "at": at,
        "duration": duration,
        "fade": fade,
    }


def _add_audio(
    context: StudioContext,
    edit: str,
    item: str,
    track: int | None = None,
    at: float | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    lane = int(track or 0)
    if lane >= plan.sequence.audio_tracks:
        raise ToolValidationError(
            f"The sequence has {plan.sequence.audio_tracks} audio track(s); track {lane} is not one of them"
        )
    reference = item
    if Path(item).suffix:
        source = _resolve_media(context, item)
        reference = str(source)
        if reference not in plan.footage:
            plan.footage.append(reference)
    plan.add(
        Step(action=StepKind.AUDIO, item=Path(reference).name, track=lane, at=at, duration=duration)
    )
    return {"edit": edit, "audio": Path(reference).name, "track": lane, "at": at, "duration": duration}


def _set_speed(
    context: StudioContext, edit: str, clip_name: str, factor: float, track: int | None = None
) -> dict[str, Any]:
    plan = context.board.edit(edit)
    plan.add(
        Step(action=StepKind.SPEED, clip_name=clip_name, factor=float(factor), track=int(track or 0))
    )
    return {"edit": edit, "clip_name": clip_name, "factor": float(factor)}


def _inspect_edit(context: StudioContext, edit: str) -> dict[str, Any]:
    plan = context.board.edit(edit)
    return {
        "edit": edit,
        "sequence": plan.sequence.model_dump(),
        "footage": [Path(item).name for item in plan.footage],
        "duration": plan.duration,
        "safe_area": plan.safe_area,
        "steps": [
            {
                "index": index,
                "action": step.action.value,
                "track": step.track,
                **{
                    key: getattr(step, key)
                    for key in Step.FIELDS_BY_ACTION.get(step.action, ())
                    if getattr(step, key) not in (None, "", [], {})
                },
                "note": step.note,
            }
            for index, step in enumerate(plan.steps)
        ],
        "clips_per_track": {
            str(track): _clip_count(plan, track) for track in range(plan.sequence.video_tracks)
        },
    }


def _list_effects(context: StudioContext, contains: str) -> dict[str, Any]:
    if not context.live("premiere"):
        return {
            "available": False,
            "effects": [],
            "reason": (
                "Premiere is not reachable, so its effect list cannot be read. Name "
                "an effect as Premiere spells it and the build will report whether it "
                "was found."
            ),
        }
    with context.host_lock("premiere"):
        context.adobe.premiere.connect()
        names = context.adobe.premiere.available_effects(contains)
    return {"available": True, "effects": names[:200], "count": len(names)}


def _build_in_premiere(context: StudioContext, author: str, edit: str) -> dict[str, Any]:
    plan = context.board.edit(edit)
    if not plan.steps:
        raise ToolValidationError(f"'{edit}' has no steps to build yet")
    if not context.live("premiere"):
        target = plan.save(context.path_for(f"{edit}_edit", ".json"))
        context.board.publish(
            Artefact(name=f"{edit}:plan", kind="edit", path=str(target), author=author)
        )
        return {
            "edit": edit,
            "built": False,
            "plan": str(target),
            "steps": len(plan.steps),
            "duration": plan.duration,
            "reason": (
                "Premiere is not reachable. Premiere is scripted through the extension "
                "panel, so it has to be running with the AI Newspaper Studio panel open. "
                "The edit is saved and will build unchanged once it is."
            ),
        }

    with context.host_lock("premiere"):
        premiere = context.adobe.premiere
        premiere.connect()
        if plan.footage:
            premiere.import_files([Path(item) for item in plan.footage], plan.bin)
        result = premiere.build_edit(plan)
        timeline = premiere.timeline_report()
    context.board.publish(
        Artefact(
            name=f"{edit}:timeline",
            kind="edit",
            author=author,
            detail={"steps": len(plan.steps), "failed": len(result.get("failed") or [])},
        )
    )
    return {
        "edit": edit,
        "built": True,
        "steps": len(plan.steps),
        "failed": result.get("failed") or [],
        "timeline": timeline,
        "duration": plan.duration,
    }


def _export_video(
    context: StudioContext, author: str, edit: str, preset: str, queue: bool | None
) -> dict[str, Any]:
    # Look the edit up for its own sake: exporting an edit nobody started
    # should say so rather than silently rendering whatever is on the timeline.
    context.board.edit(edit)
    target = context.path_for(edit, ".mp4")
    if not context.live("premiere"):
        return {
            "edit": edit,
            "exported": False,
            "reason": "Premiere is not reachable, so nothing can be rendered from it.",
        }
    with context.host_lock("premiere"):
        context.adobe.premiere.connect()
        result = context.adobe.premiere.export_sequence(
            target, preset=preset or None, queue=True if queue is None else bool(queue)
        )
    context.board.publish(
        Artefact(name=f"{edit}:render", kind="file", path=str(target), author=author, detail=result)
    )
    return {"edit": edit, "exported": True, "path": str(target), "result": result}


def _generate_video(
    context: StudioContext,
    author: str,
    prompt: str,
    seconds: float | None = None,
    aspect_ratio: str = "16:9",
    image_path: str = "",
    motion: str = "",
) -> dict[str, Any]:
    """Ask the configured generative service for a shot.

    The provider API is asynchronous because these calls take minutes. A
    specialist runs on a worker thread of its own with no event loop, so the
    wait is driven here and the specialist simply blocks on its tool call the
    way it does for a Photoshop build.
    """
    import asyncio

    from app.models.schemas import VideoRequest

    provider = context.video_provider
    if provider is None or not getattr(provider, "enabled", True):
        return {
            "generated": False,
            "reason": (
                "No generative video service is configured. Choose one in Settings and "
                "store its key in the credential vault."
            ),
        }
    request = VideoRequest(
        subject=prompt,
        motion=motion,
        duration_seconds=float(seconds or 5.0),
        aspect_ratio=aspect_ratio,
        language=context.language,
        reference_image=str(_resolve_media(context, image_path)) if image_path else "",
    )
    directory = context.workspace / "generated"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_slug(prompt)}.mp4"
    try:
        video = asyncio.run(provider.generate(request, target))
    except RuntimeError as exc:  # pragma: no cover - only if a loop is already running
        raise ToolValidationError(
            f"The video service could not be driven from this thread: {exc}"
        ) from exc
    context.board.publish(
        Artefact(
            name=Path(video.path).stem,
            kind="file",
            path=str(video.path),
            author=author,
            detail={"provider": video.provider, "prompt": prompt, "ai_generated": True},
        )
    )
    return {
        "generated": True,
        "path": str(video.path),
        "provider": video.provider,
        "model": video.model,
        "seconds": video.duration_seconds,
        "width": video.width,
        "height": video.height,
    }


def _slug(text: str, limit: int = 40) -> str:
    """A file-name-safe stem taken from what was asked for."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:limit].lower() or "clip"


def _save_edit(context: StudioContext, author: str, edit: str) -> dict[str, Any]:
    plan = context.board.edit(edit)
    target = plan.save(context.path_for(f"{edit}_edit", ".json"))
    context.board.publish(Artefact(name=f"{edit}:plan", kind="edit", path=str(target), author=author))
    return {"edit": edit, "path": str(target), "steps": len(plan.steps)}


# --------------------------------------------------------------- shared ----


def _register_shared(registry: ToolRegistry, context: StudioContext, author: str) -> ToolRegistry:
    """Tools every specialist has, whichever application it works in."""
    registry.register(
        Tool(
            name="resolve_format",
            description=(
                "Turn a size in words into real numbers - 'A3', 'instagram story', "
                "'50x70 cm at 300 dpi', '1080x1350'. Use this before starting anything "
                "rather than guessing pixels."
            ),
            parameters=[
                Parameter("request", "string", "The size, in whatever words fit"),
                Parameter("dpi", "integer", "Resolution to use", required=False, minimum=36, maximum=1200),
            ],
            capability="studio.read",
            handler=lambda request, dpi=None: _resolve_format(context, request, dpi),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="search_formats",
            description="Find the sizes this system knows, by name or by medium.",
            parameters=[
                Parameter("term", "string", "What to look for", required=False, default=""),
                Parameter("limit", "integer", "How many to return", required=False, minimum=1, maximum=50),
            ],
            capability="studio.read",
            handler=lambda term="", limit=None: _search_formats(context, term, limit),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="analyse_reference",
            description=(
                "Read an image or a video the client supplied and return the style it "
                "implies: palette with roles, contrast, type feeling, composition and "
                "- for a clip - its pace."
            ),
            parameters=[
                Parameter("path", "string", "The reference file"),
                Parameter("instruction", "string", "What to pay attention to", required=False, default=""),
            ],
            capability="studio.read",
            handler=lambda path, instruction="": _analyse_reference(context, author, path, instruction),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="list_artefacts",
            description=(
                "What the rest of the crew has finished and published - the files this "
                "specialist can build on."
            ),
            parameters=[
                Parameter("kind", "string", "Filter by kind", required=False, default="",
                          choices=["", "file", "design", "edit", "brief"]),
            ],
            capability="studio.read",
            handler=lambda kind="": _list_artefacts(context, kind),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="record_decision",
            description=(
                "Write down a decision and why it was made, so the finished work can be "
                "explained rather than only shown."
            ),
            parameters=[Parameter("text", "string", "The decision, in one or two sentences")],
            capability="studio.write",
            handler=lambda text: _record_decision(context, author, text),
            verifier=verify_truthy,
        )
    )
    return registry


def _resolve_format(context: StudioContext, request: str, dpi: int | None) -> dict[str, Any]:
    item = context.formats.resolve(request, dpi=dpi or None)
    return item.to_dict()


def _search_formats(context: StudioContext, term: str, limit: int | None) -> dict[str, Any]:
    found = context.formats.search(term, limit=int(limit or 12))
    return {
        "term": term,
        "formats": [
            {
                "name": item.name,
                "id": item.id,
                "medium": item.medium.value,
                "size": item.describe(),
                "aspect": item.aspect_label(),
            }
            for item in found
        ],
    }


def _analyse_reference(
    context: StudioContext, author: str, path: str, instruction: str
) -> dict[str, Any]:
    if context.analyst is None:
        raise ToolValidationError(
            "No reference analyst is configured for this run",
            recovery_action="Describe the style in words instead.",
        )
    source = _resolve_media(context, path)
    reference = context.analyst.analyze_one(
        source, instruction=instruction, language=context.language
    )
    if not reference.usable:
        return {"read": False, "path": str(source), "error": reference.error}
    context.board.publish(
        Artefact(
            name=f"reference:{source.stem}",
            kind="brief",
            path=str(source),
            author=author,
            detail=reference.brief.to_dict() if reference.brief else {},
        )
    )
    return {
        "read": True,
        "path": str(source),
        "brief": reference.brief.to_dict() if reference.brief else {},
        "measurements": (reference.video.to_dict() if reference.video else None)
        or (reference.measurements.to_dict() if reference.measurements else {}),
        "describes": reference.brief.describe() if reference.brief else "",
    }


def _list_artefacts(context: StudioContext, kind: str) -> dict[str, Any]:
    items = context.board.artefacts(kind)
    return {"count": len(items), "artefacts": [item.to_dict() for item in items]}


def _record_decision(context: StudioContext, author: str, text: str) -> dict[str, Any]:
    context.board.note(author, text)
    return {"recorded": True, "author": author, "text": text}


# ------------------------------------------------------- pages (InDesign) ---


def build_page_tools(
    context: StudioContext,
    policy: PermissionPolicy | None = None,
    bus: EventBus | None = None,
    *,
    author: str = "indesign",
) -> ToolRegistry:
    """The InDesign specialist's tools: a document, its pages and its frames.

    This is the surface for work that has no project behind it - a poster, a
    programme, a one-off spread. The edition pipeline has its own toolset in
    :mod:`app.agents.toolset`; this one starts from a size rather than from a
    template and a database.
    """
    registry = ToolRegistry(policy or PermissionPolicy(_studio_all()), bus or context.bus)

    registry.register(
        Tool(
            name="start_document",
            description=(
                "Begin an InDesign document at a named size, with a column grid and "
                "margins in millimetres."
            ),
            parameters=[
                Parameter("name", "string", "What to call this document"),
                Parameter("format", "string", "Preset name or explicit size"),
                Parameter("pages", "integer", "How many pages", required=False, minimum=1, maximum=200),
                Parameter("columns", "integer", "Columns in the grid", required=False, minimum=1, maximum=24),
                Parameter("gutter_mm", "number", "Space between columns", required=False, minimum=0, maximum=30),
                Parameter("margin_top_mm", "number", "Top margin", required=False, minimum=0),
                Parameter("margin_bottom_mm", "number", "Bottom margin", required=False, minimum=0),
                Parameter("margin_inside_mm", "number", "Inside margin", required=False, minimum=0),
                Parameter("margin_outside_mm", "number", "Outside margin", required=False, minimum=0),
                Parameter("facing_pages", "boolean", "Spreads rather than single pages", required=False),
                Parameter(
                    "product_type", "string", "What it is", required=False,
                    choices=["newspaper", "magazine", "brochure", "catalog", "flyer", "poster", "digital"],
                ),
            ],
            capability="design.write",
            handler=lambda **kwargs: _start_document(context, author, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_page",
            description="Add a page to the document.",
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("section", "string", "Section name", required=False, default=""),
            ],
            capability="design.write",
            handler=lambda document, section="": _add_page(context, document, section),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_text_frame",
            description=(
                "Put a text frame on a page. The style decides the typography, and the "
                "engine fits the copy to the frame and reports any overflow."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("page", "integer", "Page number", minimum=1, maximum=200),
                Parameter("name", "string", "Frame name"),
                Parameter("text", "string", "The copy"),
                Parameter("x_mm", "number", "Left edge"),
                Parameter("y_mm", "number", "Top edge"),
                Parameter("width_mm", "number", "Width", minimum=1),
                Parameter("height_mm", "number", "Height", minimum=1),
                Parameter(
                    "style", "string", "Which kind of text this is", required=False,
                    choices=_ELEMENT_STYLES, default="body",
                ),
                Parameter("columns", "integer", "Columns inside the frame", required=False, minimum=1, maximum=8),
            ],
            capability="design.write",
            handler=lambda **kwargs: _add_text_frame(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_picture_frame",
            description=(
                "Put a picture on a page. The file may be a path, or the name of "
                "something another specialist published - which is how a Photoshop "
                "design gets into the layout."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("page", "integer", "Page number", minimum=1, maximum=200),
                Parameter("name", "string", "Frame name"),
                Parameter("path", "string", "Image file, or a published artefact name"),
                Parameter("x_mm", "number", "Left edge"),
                Parameter("y_mm", "number", "Top edge"),
                Parameter("width_mm", "number", "Width", minimum=1),
                Parameter("height_mm", "number", "Height", minimum=1),
                Parameter(
                    "fit", "string", "How the picture fills the frame", required=False,
                    choices=["fill", "fit", "proportional", "none"], default="fill",
                ),
                Parameter(
                    "caption", "string",
                    "Caption text, set in its own frame under the picture",
                    required=False, default="",
                ),
            ],
            capability="design.write",
            handler=lambda **kwargs: _add_picture_frame(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="add_page_furniture",
            description=(
                "Design a piece of furniture in Photoshop and place it on the page - a "
                "tint panel behind a sidebar, a ruled box round a fact list, a quote "
                "plate, a section tab, a caption bar. This is how the boxes a newspaper "
                "page is made of are built rather than faked."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("page", "integer", "Page number", minimum=1, maximum=200),
                Parameter("name", "string", "Frame name"),
                Parameter("kind", "string", "Which piece", choices=[f.value for f in Furniture]),
                Parameter("x_mm", "number", "Left edge"),
                Parameter("y_mm", "number", "Top edge"),
                Parameter("width_mm", "number", "Width", minimum=1),
                Parameter("height_mm", "number", "Height", minimum=1),
                Parameter("color", "string", "Panel colour", required=False, default="#f2f0eb"),
                Parameter("accent", "string", "Accent colour", required=False, default="#c2410c"),
                Parameter("ink", "string", "Ink colour", required=False, default="#111111"),
                Parameter("corner_mm", "number", "Corner radius", required=False, minimum=0),
                Parameter("rule_pt", "number", "Rule weight in points", required=False, minimum=0),
                Parameter("opacity", "number", "Opacity", required=False, minimum=0, maximum=100),
            ],
            capability="design.write",
            handler=lambda **kwargs: _add_page_furniture(context, author, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="move_frame",
            description="Move or resize a frame that is already on a page.",
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("frame", "string", "Frame name"),
                Parameter("x_mm", "number", "New left edge", required=False),
                Parameter("y_mm", "number", "New top edge", required=False),
                Parameter("width_mm", "number", "New width", required=False, minimum=1),
                Parameter("height_mm", "number", "New height", required=False, minimum=1),
            ],
            capability="design.write",
            handler=lambda **kwargs: _move_frame(context, **kwargs),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="thread_frames",
            description=(
                "Link two text frames so a story runs from one into the next - which is "
                "how a column continues, and how copy that overruns one frame is carried "
                "rather than lost."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("from_frame", "string", "The frame the story starts in"),
                Parameter("to_frame", "string", "The frame it continues into"),
            ],
            capability="design.write",
            handler=lambda document, from_frame, to_frame: _thread_frames(
                context, document, from_frame, to_frame
            ),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="remove_frame",
            description="Take a frame off the page.",
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("frame", "string", "Frame name"),
            ],
            capability="design.write",
            handler=lambda document, frame: _remove_frame(context, document, frame),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="inspect_document",
            description=(
                "Read the document back: every page, its live area and every frame with "
                "its measured position, its style and whether its copy fits."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("page", "integer", "One page only", required=False, minimum=1, maximum=200),
            ],
            capability="design.read",
            handler=lambda document, page=None: _inspect_document(context, document, page),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="preview_page",
            description=(
                "Draw a page to a PNG - from InDesign when it is reachable, otherwise "
                "from the built-in renderer - so it can be looked at."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter("page", "integer", "Page number", minimum=1, maximum=200),
            ],
            capability="design.read",
            handler=lambda document, page: _preview_page(context, author, document, page),
            verifier=verify_exists,
        )
    )

    registry.register(
        Tool(
            name="build_in_indesign",
            description=(
                "Build the whole document in InDesign for real and report what it "
                "actually produced, including any frame whose copy overflows."
            ),
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter(
                    "into_open_document", "boolean",
                    "Build into the document already open in InDesign rather than a new one",
                    required=False,
                ),
            ],
            capability="design.build",
            handler=lambda document, into_open_document=None: _build_in_indesign(
                context, author, document, into_open_document
            ),
            verifier=verify_truthy,
        )
    )

    registry.register(
        Tool(
            name="export_document_pdf",
            description="Export the document as a PDF.",
            parameters=[
                Parameter("document", "string", "Which document"),
                Parameter(
                    "preset", "string", "Which output", required=False,
                    choices=["print", "web", "archive"], default="print",
                ),
            ],
            capability="design.build",
            handler=lambda document, preset="print": _export_document_pdf(context, author, document, preset),
            verifier=verify_exists,
        )
    )

    _register_shared(registry, context, author)
    return registry


@dataclass
class Document:
    """An InDesign document a specialist is building."""

    name: str
    plan: LayoutPlan
    template: TemplateSpec
    engine: LayoutEngine

    def page(self, index: int) -> PageLayout:
        """One page, or an error naming the pages there are."""
        page = self.plan.page(index)
        if page is None:
            raise ToolValidationError(
                f"'{self.name}' has no page {index}",
                context={"pages": [p.index for p in self.plan.pages]},
            )
        return page

    def frame(self, name: str) -> tuple[PageLayout, ElementSpec]:
        """A frame and the page it is on."""
        for page in self.plan.pages:
            for element in page.elements:
                if element.id == name or element.frame_name == name:
                    return (page, element)
        raise ToolValidationError(
            f"'{self.name}' has no frame called '{name}'",
            context={
                "frames": [e.id for p in self.plan.pages for e in p.elements][:40],
            },
        )


def _documents(context: StudioContext) -> dict[str, Document]:
    """The documents this run is building, made once."""
    with context._lock_guard:  # noqa: SLF001 - the context's own guard
        store = context._documents  # noqa: SLF001
        if store is None:
            store = context._documents = {}  # noqa: SLF001
        return store


def _document(context: StudioContext, name: str) -> Document:
    """One document, or an error naming the ones that exist."""
    store = _documents(context)
    document = store.get(name)
    if document is None:
        raise ToolValidationError(
            f"There is no document called '{name}'",
            context={"documents": sorted(store)},
            recovery_action="Call start_document first, or use one of the existing names.",
        )
    return document


def _studio_template(
    context: StudioContext,
    item: Any,
    *,
    product_type: str,
    columns: int,
    gutter_mm: float,
    margins: MarginSpec,
    facing_pages: bool,
) -> TemplateSpec:
    """A template for a one-off document.

    The paragraph styles, colours and PDF presets come from the built-in
    template for this language - a poster should inherit a working type scale
    rather than a set of defaults nobody chose - and only the sheet itself is
    replaced by what was asked for.
    """
    base = context.style_template
    if base is not None:
        spec = base.model_copy(deep=True)
    else:
        spec = TemplateSpec(id="studio", name="Studio")
    spec.id = f"studio_{_slug(item.name)}"
    spec.name = f"{item.name} ({product_type})"
    spec.product_type = product_type  # type: ignore[assignment]
    spec.language = context.language  # type: ignore[assignment]
    spec.direction = "rtl" if context.language in ("fa", "ar") else "ltr"  # type: ignore[assignment]
    spec.page_width_mm = round(item.width_mm, 3)
    spec.page_height_mm = round(item.height_mm, 3)
    spec.bleed_mm = item.bleed_mm if item.is_physical else 0.0
    spec.facing_pages = facing_pages
    spec.margins = margins
    spec.grid = GridSpec(
        columns=columns,
        gutter_mm=gutter_mm,
        baseline_mm=spec.grid.baseline_mm,
        rows=spec.grid.rows,
    )
    # The master pages of the source template are sized for its own sheet, so
    # they would put furniture in the wrong place on this one.
    spec.master_pages = []
    spec.indesign_template_path = None
    return spec


def _start_document(
    context: StudioContext,
    author: str,
    name: str,
    format: str,  # noqa: A002 - the tool's own argument name
    pages: int | None = None,
    columns: int | None = None,
    gutter_mm: float | None = None,
    margin_top_mm: float | None = None,
    margin_bottom_mm: float | None = None,
    margin_inside_mm: float | None = None,
    margin_outside_mm: float | None = None,
    facing_pages: bool | None = None,
    product_type: str | None = None,
) -> dict[str, Any]:
    item = context.formats.resolve(format)
    if not item.is_physical:
        log.info("'%s' is a screen format; the document is built at its pixel size in mm", item.name)
    default_margin = round(min(item.width_mm, item.height_mm) * 0.05, 1)
    margins = MarginSpec(
        top=default_margin if margin_top_mm is None else float(margin_top_mm),
        bottom=default_margin if margin_bottom_mm is None else float(margin_bottom_mm),
        inside=default_margin if margin_inside_mm is None else float(margin_inside_mm),
        outside=default_margin if margin_outside_mm is None else float(margin_outside_mm),
    )
    count = max(1, int(pages or 1))
    template = _studio_template(
        context,
        item,
        product_type=product_type or ("poster" if count == 1 else "magazine"),
        columns=int(columns or (1 if count == 1 else 6)),
        gutter_mm=4.0 if gutter_mm is None else float(gutter_mm),
        margins=margins,
        facing_pages=bool(facing_pages) if facing_pages is not None else count > 1,
    )
    _validate_margins(template)
    plan = LayoutPlan(project_id=0, template_id=template.id, language=context.language)
    for index in range(1, count + 1):
        plan.pages.append(_blank_page(template, index))
    document = Document(name=name, plan=plan, template=template, engine=LayoutEngine(template))
    _documents(context)[name] = document
    context.board.note(author, f"Started the document '{name}' at {item.describe()}, {count} page(s)")
    live = plan.pages[0].content_rect
    return {
        "document": name,
        "format": item.name,
        "page_width_mm": template.page_width_mm,
        "page_height_mm": template.page_height_mm,
        "pages": count,
        "columns": template.grid.columns,
        "gutter_mm": template.grid.gutter_mm,
        "column_width_mm": round(plan.pages[0].column_width(), 2),
        "live_area_mm": {
            "x": round(live.x, 2),
            "y": round(live.y, 2),
            "width": round(live.width, 2),
            "height": round(live.height, 2),
        },
        "styles": [style.id for style in template.paragraph_styles],
    }


def _validate_margins(template: TemplateSpec) -> None:
    """Refuse margins that would leave no page to work on."""
    live_width = template.page_width_mm - template.margins.inside - template.margins.outside
    live_height = template.page_height_mm - template.margins.top - template.margins.bottom
    if live_width < 10 or live_height < 10:
        raise ToolValidationError(
            "Those margins leave almost nothing of the page: "
            f"{live_width:.0f} x {live_height:.0f} mm inside a "
            f"{template.page_width_mm:.0f} x {template.page_height_mm:.0f} mm sheet",
            recovery_action="Use smaller margins.",
        )


def _blank_page(template: TemplateSpec, index: int) -> PageLayout:
    """An empty page carrying the template's own margins and grid."""
    top, bottom, inside, outside = template.margins_for(index)
    return PageLayout(
        index=index,
        width_mm=template.page_width_mm,
        height_mm=template.page_height_mm,
        margin_top_mm=top,
        margin_bottom_mm=bottom,
        margin_inside_mm=inside,
        margin_outside_mm=outside,
        bleed_mm=template.bleed_mm,
        columns=template.grid.columns,
        gutter_mm=template.grid.gutter_mm,
    )


def _add_page(context: StudioContext, document: str, section: str) -> dict[str, Any]:
    doc = _document(context, document)
    index = max((page.index for page in doc.plan.pages), default=0) + 1
    page = _blank_page(doc.template, index)
    page.section = section
    doc.plan.pages.append(page)
    return {"document": document, "page": index, "pages": len(doc.plan.pages)}


def _guard_frame(page: PageLayout, rect: Rect, *, ignore: str = "") -> None:
    """Refuse a frame that leaves the page or lands on top of another."""
    if not page.page_rect.contains(rect, tolerance=0.5):
        raise ToolValidationError(
            f"The frame would fall outside page {page.index} "
            f"({page.width_mm:.0f} x {page.height_mm:.0f} mm)"
        )
    for other in page.elements:
        if other.id == ignore or other.type is ElementType.RULE or other.locked:
            continue
        # Furniture is meant to sit under the copy it decorates, so it is not
        # a collision - that is the whole point of a tint panel.
        if str(other.meta.get("role", "")).startswith("furniture"):
            continue
        if other.rect.overlaps(rect, tolerance=0.6):
            raise ToolValidationError(
                f"The frame would overlap '{other.id}'; move or resize it first",
                context={
                    "occupied": {
                        "x": round(other.rect.x, 1),
                        "y": round(other.rect.y, 1),
                        "width": round(other.rect.width, 1),
                        "height": round(other.rect.height, 1),
                    }
                },
            )


def _next_z(page: PageLayout) -> int:
    """Painting order for a new frame."""
    return max((element.z_index for element in page.elements), default=0) + 1


def _add_text_frame(
    context: StudioContext,
    document: str,
    page: int,
    name: str,
    text: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    style: str = "body",
    columns: int | None = None,
) -> dict[str, Any]:
    doc = _document(context, document)
    sheet = doc.page(page)
    rect = Rect(x=x_mm, y=y_mm, width=width_mm, height=height_mm)
    _guard_frame(sheet, rect, ignore=name)
    element_type = ElementType(style)
    span = max(1, int(columns or 1))
    fit = doc.engine.typography.fit(text, rect, element_type, columns=span, allow_truncate=False)
    element = ElementSpec(
        id=name,
        type=element_type,
        rect=rect,
        z_index=_next_z(sheet),
        text=text,
        style_id=style,
        column_span=span,
        typography=fit.typography,
        estimated_overflow=fit.overflow,
    )
    sheet.elements = [item for item in sheet.elements if item.id != name]
    sheet.elements.append(element)
    return {
        "document": document,
        "page": page,
        "frame": name,
        "style": style,
        "font": fit.typography.font_family,
        "size_pt": fit.typography.size_pt,
        "leading_pt": fit.typography.leading_pt,
        "alignment": fit.typography.alignment,
        "overflow": round(fit.overflow, 3),
        "fits": fit.overflow <= 0.001,
        "characters": len(text),
    }


def _add_picture_frame(
    context: StudioContext,
    document: str,
    page: int,
    name: str,
    path: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    fit: str = "fill",
    caption: str = "",
) -> dict[str, Any]:
    doc = _document(context, document)
    sheet = doc.page(page)
    rect = Rect(x=x_mm, y=y_mm, width=width_mm, height=height_mm)
    _guard_frame(sheet, rect, ignore=name)
    source = _resolve_media(context, path)
    element = ElementSpec(
        id=name,
        type=ElementType.IMAGE,
        rect=rect,
        z_index=_next_z(sheet),
        image_path=str(source),
        fit_mode=fit,  # type: ignore[arg-type]
        style_id="image",
    )
    sheet.elements = [item for item in sheet.elements if item.id != name]
    sheet.elements.append(element)
    report = {
        "document": document,
        "page": page,
        "frame": name,
        "path": str(source),
        "fit": fit,
    }
    if caption:
        # A caption is a frame of its own, sitting under the picture: that is
        # how it is set in InDesign, and it keeps its own paragraph style.
        depth = max(6.0, doc.engine.typography.resolve(ElementType.CAPTION).leading_pt / 72 * 25.4 * 1.6)
        caption_rect = Rect(x=rect.x, y=rect.y + rect.height + 1.0, width=rect.width, height=depth)
        if sheet.page_rect.contains(caption_rect, tolerance=0.5):
            caption_fit = doc.engine.typography.fit(
                caption, caption_rect, ElementType.CAPTION, allow_truncate=False
            )
            sheet.elements = [item for item in sheet.elements if item.id != f"{name}_caption"]
            sheet.elements.append(
                ElementSpec(
                    id=f"{name}_caption",
                    type=ElementType.CAPTION,
                    rect=caption_rect,
                    z_index=_next_z(sheet),
                    text=caption,
                    style_id="caption",
                    typography=caption_fit.typography,
                    estimated_overflow=caption_fit.overflow,
                )
            )
            report["caption"] = caption
            report["caption_frame"] = f"{name}_caption"
        else:
            report["caption"] = caption
            report["caption_warning"] = (
                "There is no room under the picture for its caption; move the frame up "
                "or set the caption separately."
            )
    dpi = _effective_dpi(source, width_mm)
    if dpi is not None:
        report["effective_dpi"] = dpi
        if dpi < doc.template.layout_rules.min_image_dpi:
            report["warning"] = (
                f"At {width_mm:.0f} mm wide this picture prints at {dpi:.0f} dpi, below the "
                f"{doc.template.layout_rules.min_image_dpi:.0f} dpi this template asks for."
            )
    return report


def _effective_dpi(source: Path, width_mm: float) -> float | None:
    """What the picture will actually print at, when it can be measured."""
    try:
        from PIL import Image

        with Image.open(source) as image:
            pixels = image.size[0]
    except Exception as exc:  # noqa: BLE001 - a video or a missing decoder
        log.debug("Could not measure %s: %s", source.name, exc)
        return None
    if width_mm <= 0:
        return None
    return round(pixels / (width_mm / 25.4), 1)


def _add_page_furniture(
    context: StudioContext,
    author: str,
    document: str,
    page: int,
    name: str,
    kind: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    color: str = "#f2f0eb",
    accent: str = "#c2410c",
    ink: str = "#111111",
    corner_mm: float | None = None,
    rule_pt: float | None = None,
    opacity: float | None = None,
) -> dict[str, Any]:
    doc = _document(context, document)
    sheet = doc.page(page)
    spec = FurnitureSpec(
        kind=Furniture(kind),
        width_mm=float(width_mm),
        height_mm=float(height_mm),
        dpi=context.dpi,
        color=_hex(color, "color"),
        accent=_hex(accent, "accent"),
        ink=_hex(ink, "ink"),
        corner_mm=float(corner_mm or 0.0),
        rule_pt=float(rule_pt) if rule_pt is not None else 0.75,
        opacity=100.0 if opacity is None else float(opacity),
        direction="rtl" if context.language in ("fa", "ar") else "ltr",
    )
    factory = _furniture_factory(context)
    with context.host_lock("photoshop"):
        target = factory.make(spec)
    placed = factory.placement(
        spec, Box(x=x_mm, y=y_mm, width=width_mm, height=height_mm), context.dpi
    )
    rect = Rect(x=placed.x, y=placed.y, width=placed.width, height=placed.height)
    if not sheet.page_rect.contains(rect, tolerance=2.0):
        # The piece is drawn a couple of millimetres over its frame so an edge
        # effect is not clipped; at the very edge of the sheet that overhang is
        # the bleed, which is exactly where it belongs.
        log.info("The %s on page %d bleeds off the trim", kind, page)
    element = ElementSpec(
        id=name,
        type=ElementType.IMAGE,
        rect=rect,
        z_index=min((item.z_index for item in sheet.elements), default=1) - 1,
        image_path=str(target),
        fit_mode="fill",
        style_id="image",
        locked=False,
        meta={"role": f"furniture:{kind}", "designed_in": "photoshop"},
    )
    sheet.elements = [item for item in sheet.elements if item.id != name]
    sheet.elements.append(element)
    context.board.publish(
        Artefact(
            name=f"{document}:{name}",
            kind="file",
            path=str(target),
            author=author,
            detail={"furniture": kind, "page": page},
        )
    )
    return {
        "document": document,
        "page": page,
        "frame": name,
        "furniture": kind,
        "path": str(target),
        "x_mm": round(rect.x, 2),
        "y_mm": round(rect.y, 2),
        "width_mm": round(rect.width, 2),
        "height_mm": round(rect.height, 2),
        "behind": True,
    }


def _move_frame(
    context: StudioContext,
    document: str,
    frame: str,
    x_mm: float | None = None,
    y_mm: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
) -> dict[str, Any]:
    doc = _document(context, document)
    page, element = doc.frame(frame)
    if element.locked:
        raise ToolValidationError(f"'{frame}' is locked")
    candidate = Rect(
        x=element.rect.x if x_mm is None else float(x_mm),
        y=element.rect.y if y_mm is None else float(y_mm),
        width=element.rect.width if width_mm is None else float(width_mm),
        height=element.rect.height if height_mm is None else float(height_mm),
    )
    _guard_frame(page, candidate, ignore=element.id)
    element.rect = candidate
    if element.is_text and element.text:
        fit = doc.engine.typography.fit(
            element.text,
            candidate,
            element.type,
            columns=max(1, element.column_span),
            allow_truncate=False,
        )
        element.typography = fit.typography
        element.estimated_overflow = fit.overflow
    return {
        "document": document,
        "frame": frame,
        "page": page.index,
        "x_mm": round(candidate.x, 2),
        "y_mm": round(candidate.y, 2),
        "width_mm": round(candidate.width, 2),
        "height_mm": round(candidate.height, 2),
        "overflow": round(element.estimated_overflow, 3),
    }


def _thread_frames(
    context: StudioContext, document: str, from_frame: str, to_frame: str
) -> dict[str, Any]:
    doc = _document(context, document)
    first_page, first = doc.frame(from_frame)
    _second_page, second = doc.frame(to_frame)
    if not (first.is_text and second.is_text):
        raise ToolValidationError("Only text frames can be threaded")
    if first.id == second.id:
        raise ToolValidationError("A frame cannot continue into itself")
    if second.meta.get("threads_from") not in (None, from_frame):
        raise ToolValidationError(
            f"'{to_frame}' already continues '{second.meta['threads_from']}'; "
            "a frame belongs to one story"
        )
    if _would_loop(doc, to_frame, from_frame):
        raise ToolValidationError(
            f"Threading '{from_frame}' into '{to_frame}' would make the story run in a circle"
        )
    head = _story_head(doc, first)
    if second.text.strip() and second.meta.get("threads_from") is None:
        raise ToolValidationError(
            f"'{to_frame}' already carries copy of its own; empty it first, or thread into "
            "a frame that is waiting for the story"
        )

    chain = [name for name in (first.meta.get("threads_to") or []) if name != to_frame]
    chain.append(to_frame)
    first.meta["threads_to"] = chain
    second.meta["threads_from"] = from_frame
    second.type = head.type
    second.style_id = head.style_id
    second.column_span = second.column_span or head.column_span

    flowed = _flow_story(doc, head)
    _apply_threading(context, doc, head)
    return {
        "document": document,
        "from": from_frame,
        "to": to_frame,
        "page": first_page.index,
        "story": [item["frame"] for item in flowed],
        "frames": flowed,
        "overflow": flowed[-1]["overflow"] if flowed else 0.0,
        "fits": bool(flowed) and flowed[-1]["overflow"] <= 0.001,
    }


def _story_head(doc: Document, element: ElementSpec) -> ElementSpec:
    """The frame a story starts in, following the chain back."""
    seen: set[str] = set()
    current = element
    while True:
        previous = current.meta.get("threads_from")
        if not previous or previous in seen:
            return current
        seen.add(str(previous))
        try:
            _page, current = doc.frame(str(previous))
        except ToolValidationError:
            return current


def _story_chain(doc: Document, head: ElementSpec) -> list[ElementSpec]:
    """Every frame of a story, in reading order."""
    chain = [head]
    seen = {head.id}
    current = head
    while True:
        following = [name for name in (current.meta.get("threads_to") or []) if name not in seen]
        if not following:
            return chain
        try:
            _page, current = doc.frame(str(following[0]))
        except ToolValidationError:
            return chain
        seen.add(current.id)
        chain.append(current)


def _flow_story(doc: Document, head: ElementSpec) -> list[dict[str, Any]]:
    """Distribute a story's copy across its threaded frames.

    InDesign does this itself when it is driving; offline the preview is the
    only view there is, so the copy is split here by measuring rather than
    left sitting entirely in the first frame with the rest blank.
    """
    chain = _story_chain(doc, head)
    story = str(head.meta.get("story") or head.text)
    head.meta["story"] = story
    words = story.split()
    engine = doc.engine.typography
    placed: list[dict[str, Any]] = []
    index = 0

    for position, element in enumerate(chain):
        last = position == len(chain) - 1
        remaining = words[index:]
        if not remaining:
            element.text = ""
            element.estimated_overflow = 0.0
            placed.append({"frame": element.id, "words": 0, "overflow": 0.0})
            continue
        if last:
            taken = len(remaining)
        else:
            taken = _fitting_words(engine, element, remaining)
        element.text = " ".join(remaining[:taken])
        fit = engine.fit(
            element.text,
            element.rect,
            element.type,
            columns=max(1, element.column_span),
            allow_truncate=False,
        )
        element.typography = fit.typography
        element.estimated_overflow = fit.overflow
        index += taken
        placed.append(
            {"frame": element.id, "words": taken, "overflow": round(fit.overflow, 3)}
        )
    return placed


def _fitting_words(engine: Any, element: ElementSpec, words: list[str]) -> int:
    """How many of *words* this frame holds, found by bisection."""
    low, high = 0, len(words)
    while low < high:
        middle = (low + high + 1) // 2
        fit = engine.fit(
            " ".join(words[:middle]),
            element.rect,
            element.type,
            columns=max(1, element.column_span),
            allow_truncate=False,
        )
        if fit.overflow <= 0.001:
            low = middle
        else:
            high = middle - 1
    # A frame too small for even one word still takes it, or the story stalls.
    return max(1, low)


def _would_loop(doc: Document, start: str, target: str) -> bool:
    """Whether following the thread from *start* reaches *target*."""
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name == target:
            return True
        if name in seen:
            continue
        seen.add(name)
        try:
            _page, element = doc.frame(name)
        except ToolValidationError:
            continue
        queue.extend(str(item) for item in (element.meta.get("threads_to") or []))
    return False


def _apply_threading(context: StudioContext, doc: Document, head: ElementSpec) -> None:
    """Tell InDesign about the chain, when InDesign is driving."""
    if not context.live("indesign"):
        return
    chain = _story_chain(doc, head)
    with context.host_lock("indesign"):
        for first, second in zip(chain, chain[1:], strict=False):
            context.adobe.indesign.thread_frames(first.frame_name, second.frame_name)


def _remove_frame(context: StudioContext, document: str, frame: str) -> dict[str, Any]:
    doc = _document(context, document)
    page, element = doc.frame(frame)
    if element.locked:
        raise ToolValidationError(f"'{frame}' is locked")
    page.elements.remove(element)
    for other in page.elements:
        threads = other.meta.get("threads_to")
        if threads and element.id in threads:
            other.meta["threads_to"] = [name for name in threads if name != element.id]
    if context.live("indesign"):
        with context.host_lock("indesign"):
            context.adobe.indesign.delete_element(element.frame_name)
    return {"document": document, "removed": frame, "page": page.index, "frames": len(page.elements)}


def _inspect_document(context: StudioContext, document: str, page: int | None) -> dict[str, Any]:
    doc = _document(context, document)
    pages = [doc.page(int(page))] if page else doc.plan.pages
    out = []
    for sheet in pages:
        live = sheet.content_rect
        out.append(
            {
                "page": sheet.index,
                "section": sheet.section,
                "size_mm": {"width": sheet.width_mm, "height": sheet.height_mm},
                "live_area_mm": {
                    "x": round(live.x, 2),
                    "y": round(live.y, 2),
                    "width": round(live.width, 2),
                    "height": round(live.height, 2),
                },
                "columns": sheet.columns,
                "column_width_mm": round(sheet.column_width(), 2),
                "frames": [
                    {
                        "name": element.id,
                        "type": element.type.value,
                        "x_mm": round(element.rect.x, 2),
                        "y_mm": round(element.rect.y, 2),
                        "width_mm": round(element.rect.width, 2),
                        "height_mm": round(element.rect.height, 2),
                        "overflow": round(element.estimated_overflow, 3),
                        "locked": element.locked,
                        "role": element.meta.get("role", ""),
                        **(
                            {
                                "font": element.typography.font_family,
                                "size_pt": element.typography.size_pt,
                                "text": element.text[:80],
                            }
                            if element.is_text and element.typography
                            else {}
                        ),
                        **({"image": element.image_path} if element.is_image else {}),
                    }
                    for element in sorted(sheet.elements, key=lambda e: (e.z_index, e.id))
                ],
                "empty": not sheet.elements,
            }
        )
    return {
        "document": document,
        "pages": out,
        "page_count": len(doc.plan.pages),
        "frames": doc.plan.element_count(),
        "styles": [style.id for style in doc.template.paragraph_styles],
    }


def _preview_page(context: StudioContext, author: str, document: str, page: int) -> dict[str, Any]:
    doc = _document(context, document)
    sheet = doc.page(int(page))
    target = context.path_for(f"{document}_p{int(page):03d}", ".png")
    if context.live("indesign"):
        try:
            with context.host_lock("indesign"):
                context.adobe.indesign.render_preview(int(page), target, dpi=110)
            engine = "indesign"
        except Exception as exc:  # noqa: BLE001 - the built-in renderer still works
            log.warning("InDesign could not render page %d (%s); the built-in renderer did", page, exc)
            engine = "builtin"
        else:
            context.board.publish(
                Artefact(name=f"{document}:p{page}", kind="file", path=str(target), author=author)
            )
            return {"document": document, "page": int(page), "path": str(target), "engine": engine}

    from app.vision.renderer import PreviewRenderer

    result = PreviewRenderer(doc.template).render_page(sheet, target)
    context.board.publish(
        Artefact(name=f"{document}:p{page}", kind="file", path=str(result.path), author=author)
    )
    return {
        "document": document,
        "page": int(page),
        "path": str(result.path),
        "engine": "builtin",
        "warnings": result.warnings,
    }


def _build_in_indesign(
    context: StudioContext, author: str, document: str, into_open_document: bool | None
) -> dict[str, Any]:
    doc = _document(context, document)
    if not doc.plan.element_count():
        raise ToolValidationError(f"'{document}' has no frames to build yet")
    if not context.live("indesign"):
        target = doc.plan.save(context.path_for(f"{document}_layout", ".json"))
        context.board.publish(
            Artefact(name=f"{document}:plan", kind="design", path=str(target), author=author)
        )
        return {
            "document": document,
            "built": False,
            "plan": str(target),
            "pages": len(doc.plan.pages),
            "reason": (
                "InDesign is not reachable on this machine. The layout is saved and will "
                "build unchanged once it is; preview_page and export_document_pdf still "
                "produce files from the built-in renderer."
            ),
        }
    with context.host_lock("indesign"):
        indesign = context.adobe.indesign
        indesign.connect()
        result = indesign.build_document(
            doc.plan, doc.template, use_open_document=bool(into_open_document)
        )
        overflow = indesign.detect_overflow()
    context.board.publish(
        Artefact(
            name=f"{document}:document",
            kind="design",
            author=author,
            detail={"pages": len(doc.plan.pages), "overflow": len(overflow)},
        )
    )
    return {
        "document": document,
        "built": True,
        "pages": len(doc.plan.pages),
        "frames": doc.plan.element_count(),
        "overflow": overflow,
        "result": {key: value for key, value in result.items() if key != "pages"},
    }


def _export_document_pdf(
    context: StudioContext, author: str, document: str, preset: str
) -> dict[str, Any]:
    doc = _document(context, document)
    target = context.path_for(f"{document}_{preset}", ".pdf")
    if context.live("indesign"):
        spec = next(
            (item for item in doc.template.pdf_presets if item.id == preset),
            doc.template.pdf_presets[0] if doc.template.pdf_presets else None,
        )
        with context.host_lock("indesign"):
            context.adobe.indesign.export_pdf(target, spec)
        engine = "indesign"
    else:
        from app.utils import imaging
        from app.vision.renderer import PreviewRenderer

        # A PDF is the deliverable, so it is rendered at print resolution
        # rather than at the resolution a screen preview needs.
        renderer = PreviewRenderer(doc.template, dpi=context.dpi)
        images = []
        for sheet in doc.plan.pages:
            page_file = context.path_for(f"{document}_pdf_p{sheet.index:03d}", ".png")
            images.append(renderer.render_page(sheet, page_file).path)
        imaging.write_pdf(images, target, context.dpi, context.dpi)
        engine = "builtin"
    context.board.publish(
        Artefact(name=f"{document}:pdf", kind="file", path=str(target), author=author,
                 detail={"engine": engine, "preset": preset})
    )
    return {"document": document, "path": str(target), "engine": engine, "preset": preset}
