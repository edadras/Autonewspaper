"""What a design is, before any application has been asked to build it.

A :class:`DesignPlan` is to a poster or a social post what a layout plan is to
a newspaper page: a complete, measurable description that can be rendered for
review, checked by QA, and handed to Photoshop (or, for a print piece,
InDesign) to be built for real.

Everything is in pixels from the top-left of the canvas, because that is what
both Photoshop and every screen format work in; a print format converts on
its way in through :meth:`DesignPlan.for_format`.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.formats import Format, Medium


class LayerKind(str, Enum):
    """What a layer is."""

    TEXT = "text"
    SHAPE = "shape"
    IMAGE = "image"
    GROUP = "group"


class ShapeKind(str, Enum):
    """The shapes the design engine can draw."""

    RECTANGLE = "rectangle"
    ROUNDED = "rounded"
    ELLIPSE = "ellipse"
    POLYGON = "polygon"


class Box(BaseModel):
    """A rectangle in pixels, measured from the top-left of the canvas."""

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    @property
    def right(self) -> float:
        """Right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """Bottom edge."""
        return self.y + self.height

    @property
    def area(self) -> float:
        """Area in square pixels."""
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        """Centre point."""
        return (self.x + self.width / 2, self.y + self.height / 2)

    def overlaps(self, other: Box) -> float:
        """Area shared with *other*."""
        across = min(self.right, other.right) - max(self.x, other.x)
        down = min(self.bottom, other.bottom) - max(self.y, other.y)
        return max(0.0, across) * max(0.0, down)

    def contains(self, other: Box, tolerance: float = 0.5) -> bool:
        """Whether *other* sits entirely inside this box."""
        return (
            other.x >= self.x - tolerance
            and other.y >= self.y - tolerance
            and other.right <= self.right + tolerance
            and other.bottom <= self.bottom + tolerance
        )


class Effects(BaseModel):
    """Layer styles. Each is optional; an empty model applies nothing."""

    shadow: dict[str, Any] | None = None
    inner_shadow: dict[str, Any] | None = None
    glow: dict[str, Any] | None = None
    stroke: dict[str, Any] | None = None
    overlay: dict[str, Any] | None = None
    gradient: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        """Whether nothing is set."""
        return not any(self.model_dump().values())


class Layer(BaseModel):
    """One layer of a design."""

    name: str
    kind: LayerKind
    box: Box = Field(default_factory=Box)
    z: int = 0
    """Painting order: lower is further back."""
    opacity: float = 100.0
    blend_mode: str = "normal"
    rotation: float = 0.0
    group: str | None = None
    clip_to_below: bool = False
    effects: Effects = Field(default_factory=Effects)
    locked: bool = False
    role: str = ""
    """What this layer is for - ``headline``, ``subject``, ``logo`` - used by
    the design rules and by the quality check."""

    # --- text ------------------------------------------------------------
    text: str = ""
    font: str = ""
    font_style: str = "Regular"
    fallback_fonts: list[str] = Field(default_factory=list)
    size_pt: float = 0.0
    leading_pt: float = 0.0
    tracking: float = 0.0
    color: str = "#000000"
    alignment: Literal["left", "center", "right", "justify"] = "left"
    direction: Literal["ltr", "rtl"] = "ltr"
    all_caps: bool = False

    # --- shape -----------------------------------------------------------
    shape: ShapeKind = ShapeKind.RECTANGLE
    radius: float = 0.0
    points: list[tuple[float, float]] = Field(default_factory=list)

    # --- image -----------------------------------------------------------
    path: str = ""
    fit: Literal["cover", "contain", "stretch"] = "cover"
    linked: bool = False

    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        """Whether this layer carries type."""
        return self.kind is LayerKind.TEXT

    @property
    def is_image(self) -> bool:
        """Whether this layer carries a picture."""
        return self.kind is LayerKind.IMAGE

    def to_photoshop(self) -> dict[str, Any]:
        """The shape ``AINS.PSD.buildDesign`` expects."""
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "name": self.name,
            "x": round(self.box.x, 2),
            "y": round(self.box.y, 2),
            "width": round(self.box.width, 2),
            "height": round(self.box.height, 2),
            "opacity": self.opacity,
            "blend_mode": self.blend_mode,
            "rotation": self.rotation,
            "group": self.group,
            "clip_to_below": self.clip_to_below,
        }
        if not self.effects.is_empty():
            payload["effects"] = {k: v for k, v in self.effects.model_dump().items() if v}
        if self.kind is LayerKind.TEXT:
            payload.update(
                {
                    "text": self.text,
                    "font": self.font,
                    "font_style": self.font_style,
                    "fallback_fonts": self.fallback_fonts,
                    "size_pt": self.size_pt,
                    "leading_pt": self.leading_pt or round(self.size_pt * 1.2, 2),
                    "tracking": self.tracking,
                    "color": self.color,
                    "alignment": self.alignment,
                    "direction": self.direction,
                    "all_caps": self.all_caps,
                }
            )
        elif self.kind is LayerKind.SHAPE:
            payload.update(
                {
                    "shape": self.shape.value,
                    "radius": self.radius,
                    "color": self.color,
                    "points": [list(point) for point in self.points],
                }
            )
        elif self.kind is LayerKind.IMAGE:
            payload.update({"path": self.path, "fit": self.fit, "linked": self.linked})
        elif self.kind is LayerKind.GROUP:
            payload["parent"] = self.group
        return payload


class Canvas(BaseModel):
    """The sheet a design is built on."""

    width_px: int
    height_px: int
    dpi: int = 300
    mode: Literal["rgb", "cmyk", "gray"] = "rgb"
    background: str = "#ffffff"
    format_id: str = ""
    format_name: str = ""
    medium: str = Medium.SCREEN.value
    bleed_px: int = 0
    safe_box: Box | None = None
    """Where a platform's own interface sits; nothing that must be read may
    fall outside this."""

    def to_photoshop(self, name: str = "Design") -> dict[str, Any]:
        """The shape ``AINS.PSD.createCanvas`` expects."""
        return {
            "name": name,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "mode": self.mode,
            "background": self.background,
        }

    @property
    def box(self) -> Box:
        """The whole canvas as a box."""
        return Box(x=0, y=0, width=self.width_px, height=self.height_px)


class DesignPlan(BaseModel):
    """A complete design, ready to be rendered or built."""

    project_id: int = 0
    name: str = ""
    canvas: Canvas
    layers: list[Layer] = Field(default_factory=list)
    language: str = "fa"
    palette: list[str] = Field(default_factory=list)
    fonts: dict[str, str] = Field(default_factory=dict)
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    qa_score: float = 0.0
    iterations: int = 0
    brief: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------- building
    @classmethod
    def for_format(
        cls,
        item: Format,
        *,
        name: str = "",
        language: str = "fa",
        background: str = "#ffffff",
        mode: Literal["rgb", "cmyk", "gray"] | None = None,
    ) -> DesignPlan:
        """An empty plan sized for *item*."""
        safe = item.safe_area
        safe_box = None
        if not safe.is_empty():
            x, y, width, height = safe.inset_px(item.width_px, item.height_px)
            safe_box = Box(x=x, y=y, width=width, height=height)
        colour_mode = mode or ("cmyk" if item.medium is Medium.PRINT else "rgb")
        return cls(
            name=name or item.name,
            language=language,
            canvas=Canvas(
                width_px=item.width_px,
                height_px=item.height_px,
                dpi=item.dpi,
                mode=colour_mode,
                background=background,
                format_id=item.id,
                format_name=item.name,
                medium=item.medium.value,
                bleed_px=int(round(item.bleed_mm / 25.4 * item.dpi)),
                safe_box=safe_box,
            ),
        )

    def add(self, layer: Layer) -> Layer:
        """Append a layer, giving it the next painting order."""
        if not layer.z:
            layer.z = (max((existing.z for existing in self.layers), default=0)) + 10
        self.layers.append(layer)
        return layer

    def layer(self, name: str) -> Layer | None:
        """Find a layer by name."""
        return next((item for item in self.layers if item.name == name), None)

    def by_role(self, role: str) -> list[Layer]:
        """Every layer with a given role."""
        return [item for item in self.layers if item.role == role]

    def ordered(self) -> list[Layer]:
        """Layers back to front, which is the order they are built in."""
        return sorted(self.layers, key=lambda item: (item.z, item.name))

    # --------------------------------------------------------------- output
    def to_photoshop(self) -> dict[str, Any]:
        """The whole plan in the shape ``AINS.PSD.buildDesign`` expects."""
        return {
            "canvas": self.canvas.to_photoshop(self.name or "Design"),
            "layers": [layer.to_photoshop() for layer in self.ordered()],
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
    def load(cls, path: Path | str) -> DesignPlan:
        """Read a plan back."""
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
