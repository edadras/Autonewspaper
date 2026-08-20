"""Template specification.

A template describes everything about a publication's *design system* that is
independent of a particular edition: trim size, grid, margins, bleed, master
pages, colours, paragraph/character/object styles and the layout rules the
engine must respect. Templates are plain JSON (``*.template.json``) so they
can be shared, versioned and edited outside the application.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.errors import TemplateError
from app.models.schemas import ElementType, TypographySpec

TEMPLATE_SCHEMA_VERSION = "1.0"


class ColorSpec(BaseModel):
    """A named colour, defined in CMYK for print and mirrored to RGB."""

    name: str
    cyan: float = Field(0.0, ge=0, le=100)
    magenta: float = Field(0.0, ge=0, le=100)
    yellow: float = Field(0.0, ge=0, le=100)
    black: float = Field(100.0, ge=0, le=100)
    spot: bool = False

    def to_rgb(self) -> tuple[int, int, int]:
        """Naive CMYK->RGB conversion, used by the internal preview renderer."""
        c, m, y, k = (self.cyan / 100, self.magenta / 100, self.yellow / 100, self.black / 100)
        return (
            int(round(255 * (1 - c) * (1 - k))),
            int(round(255 * (1 - m) * (1 - k))),
            int(round(255 * (1 - y) * (1 - k))),
        )

    def to_hex(self) -> str:
        """``#rrggbb`` form."""
        r, g, b = self.to_rgb()
        return f"#{r:02x}{g:02x}{b:02x}"


class MarginSpec(BaseModel):
    """Page margins in millimetres."""

    top: float = 15.0
    bottom: float = 15.0
    inside: float = 15.0
    outside: float = 12.0

    def max(self) -> float:
        """Largest margin, used for sanity checks."""
        return max(self.top, self.bottom, self.inside, self.outside)


class GridSpec(BaseModel):
    """Column grid and baseline grid."""

    columns: int = Field(6, ge=1, le=24)
    gutter_mm: float = Field(4.0, ge=0, le=30)
    baseline_mm: float = Field(4.4, gt=0)
    rows: int = Field(12, ge=1, le=60)
    """Horizontal modules used by the modular layout strategies."""

    def module_height(self, content_height_mm: float) -> float:
        """Height of one horizontal module."""
        return content_height_mm / max(1, self.rows)


#: Style ids that are meant to be set smaller than body copy.
SECONDARY_TEXT_STYLES = frozenset({"folio", "caption", "kicker", "byline"})


class ParagraphStyleSpec(TypographySpec):
    """A named paragraph style, exported to InDesign as a paragraph style."""

    id: str = "body"
    min_size_pt: float = 7.5
    max_size_pt: float = 96.0
    auto_fit: bool = True
    """Whether the typography engine may resize this style to avoid overflow."""
    rule_above_pt: float = 0.0
    rule_below_pt: float = 0.0
    drop_cap_lines: int = 0

    @model_validator(mode="after")
    def _check_size_range(self) -> ParagraphStyleSpec:
        """The size range has to contain the size the style is set at.

        An inverted range does not fail loudly: ``clamp`` simply returns the
        minimum, so a style written with ``min 12 / max 10`` silently sets
        9.5 pt body copy at 12 pt and nothing says why.
        """
        if self.min_size_pt > self.max_size_pt:
            raise ValueError(
                f"Paragraph style '{self.id}' has min_size_pt {self.min_size_pt} above "
                f"max_size_pt {self.max_size_pt}"
            )
        if not (self.min_size_pt - 0.01 <= self.size_pt <= self.max_size_pt + 0.01):
            raise ValueError(
                f"Paragraph style '{self.id}' is set at {self.size_pt} pt, outside its own "
                f"{self.min_size_pt}-{self.max_size_pt} pt range"
            )
        return self

    def clamp(self, size_pt: float) -> float:
        """Constrain a candidate size to this style's allowed range."""
        return max(self.min_size_pt, min(self.max_size_pt, size_pt))


class CharacterStyleSpec(BaseModel):
    """A named character style (bylines, emphasis, datelines)."""

    id: str
    font_family: str = ""
    font_style: str = ""
    size_pt: float | None = None
    color: str = "Black"
    tracking: float = 0.0
    all_caps: bool = False
    underline: bool = False


class ObjectStyleSpec(BaseModel):
    """A named object style applied to frames."""

    id: str
    stroke_weight_pt: float = 0.0
    stroke_color: str = "Black"
    fill_color: str | None = None
    text_wrap_mm: float = 0.0
    corner_radius_mm: float = 0.0
    inset_mm: float = 0.0


class MasterElementSpec(BaseModel):
    """A frame placed on a master page (folio, running head, rules)."""

    type: ElementType
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    text: str = ""
    style_id: str = ""
    image_path: str | None = None
    only_pages: Literal["all", "odd", "even", "first"] = "all"


class MasterPageSpec(BaseModel):
    """A master page definition."""

    name: str = "A-Master"
    applies_to: Literal["all", "odd", "even", "first"] = "all"
    elements: list[MasterElementSpec] = Field(default_factory=list)


class SlotSpec(BaseModel):
    """A named area weight used by the layout strategies."""

    area: Literal["main", "secondary", "small", "sidebar"]
    min_height_ratio: float = Field(0.12, gt=0, le=1)
    max_height_ratio: float = Field(0.55, gt=0, le=1)
    min_columns: int = 1
    max_columns: int = 6
    image_probability: float = Field(0.5, ge=0, le=1)


class LayoutRules(BaseModel):
    """Hard and soft constraints the engine must respect."""

    min_body_size_pt: float = 7.5
    min_secondary_size_pt: float = 6.0
    """Floor for the small furniture - folios, captions, kickers and credits.

    A folio is meant to be smaller than body text; holding it to the body
    minimum reports the template's own furniture as a fault.
    """
    min_image_dpi: float = 200.0
    min_element_height_mm: float = 6.0
    min_element_width_mm: float = 18.0
    max_articles_per_page: int = 6
    max_images_per_page: int = 4
    headline_min_size_pt: float = 14.0
    image_aspect_choices: list[str] = Field(default_factory=lambda: ["16:9", "4:3", "3:2", "1:1"])
    allowed_strategies: list[str] = Field(
        default_factory=lambda: ["hierarchical", "modular", "horizontal", "vertical", "feature"]
    )
    slots: list[SlotSpec] = Field(
        default_factory=lambda: [
            SlotSpec(
                area="main",
                min_height_ratio=0.30,
                max_height_ratio=0.62,
                min_columns=3,
                image_probability=0.95,
            ),
            SlotSpec(
                area="secondary",
                min_height_ratio=0.18,
                max_height_ratio=0.38,
                min_columns=2,
                image_probability=0.6,
            ),
            SlotSpec(
                area="small",
                min_height_ratio=0.10,
                max_height_ratio=0.24,
                min_columns=1,
                image_probability=0.25,
            ),
            SlotSpec(
                area="sidebar",
                min_height_ratio=0.10,
                max_height_ratio=0.55,
                min_columns=1,
                max_columns=2,
                image_probability=0.15,
            ),
        ]
    )
    keep_masthead_on_first_page: bool = True
    allow_article_continuation: bool = True
    whitespace_target: float = Field(0.14, ge=0.0, le=0.6)
    """Desired share of the live area left empty; the scorer penalises drift."""


class PDFPresetSpec(BaseModel):
    """A named PDF export preset."""

    id: str
    label: str = ""
    indesign_preset: str = "[High Quality Print]"
    color_space: Literal["CMYK", "RGB", "Gray"] = "CMYK"
    include_bleed: bool = True
    include_marks: bool = True
    downsample_dpi: int = 300
    compression: Literal["none", "zip", "jpeg", "auto"] = "auto"
    jpeg_quality: Literal["low", "medium", "high", "maximum"] = "high"
    embed_fonts: bool = True
    flatten_transparency: bool = False


class TemplateSpec(BaseModel):
    """A complete publication design system."""

    schema_version: str = TEMPLATE_SCHEMA_VERSION
    id: str
    name: str
    description: str = ""
    product_type: Literal["newspaper", "magazine", "brochure", "catalog", "flyer", "poster", "digital"] = (
        "newspaper"
    )
    language: Literal["fa", "en", "ar", "tr"] = "fa"
    direction: Literal["rtl", "ltr"] = "rtl"
    author: str = ""
    version: str = "1.0"

    page_width_mm: float = 297.0
    page_height_mm: float = 420.0
    margins: MarginSpec = Field(default_factory=MarginSpec)
    bleed_mm: float = Field(3.0, ge=0, le=20)
    slug_mm: float = Field(0.0, ge=0, le=30)
    facing_pages: bool = True
    grid: GridSpec = Field(default_factory=GridSpec)

    colors: list[ColorSpec] = Field(default_factory=list)
    paragraph_styles: list[ParagraphStyleSpec] = Field(default_factory=list)
    character_styles: list[CharacterStyleSpec] = Field(default_factory=list)
    object_styles: list[ObjectStyleSpec] = Field(default_factory=list)
    master_pages: list[MasterPageSpec] = Field(default_factory=list)

    fonts: dict[str, str] = Field(default_factory=dict)
    font_fallbacks: dict[str, list[str]] = Field(default_factory=dict)
    layout_rules: LayoutRules = Field(default_factory=LayoutRules)
    pdf_presets: list[PDFPresetSpec] = Field(default_factory=list)

    masthead_height_mm: float = 48.0
    logo_path: str | None = None
    indesign_template_path: str | None = None
    """Optional ``.indt``/``.indd`` opened instead of creating a blank document."""
    meta: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------ validation
    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value or not all(ch.isalnum() or ch in "_-" for ch in value):
            raise ValueError("Template id must be alphanumeric with '_' or '-'")
        return value

    @model_validator(mode="after")
    def _check_geometry(self) -> TemplateSpec:
        content_width = self.page_width_mm - self.margins.inside - self.margins.outside
        content_height = self.page_height_mm - self.margins.top - self.margins.bottom
        if content_width <= 20 or content_height <= 20:
            raise ValueError("Margins leave no usable live area on the page")
        if self.grid.columns > 1:
            usable = content_width - self.grid.gutter_mm * (self.grid.columns - 1)
            if usable / self.grid.columns < 10:
                raise ValueError("Column width would be under 10 mm; reduce columns or gutter")
        return self

    @model_validator(mode="after")
    def _check_typography(self) -> TemplateSpec:
        """A template's own styles must satisfy the rules it declares.

        The English magazine shipped with a folio set below its own body
        minimum, so every page after the first was reported as having a
        readability fault the operator could not fix - the template was the
        one at fault. A template that contradicts itself is rejected here
        rather than at layout time.
        """
        for style in self.paragraph_styles:
            if style.id in ("headline", "masthead"):
                floor, named = self.layout_rules.headline_min_size_pt, "headline_min_size_pt"
            elif style.id in SECONDARY_TEXT_STYLES:
                floor, named = self.layout_rules.min_secondary_size_pt, "min_secondary_size_pt"
            else:
                floor, named = self.layout_rules.min_body_size_pt, "min_body_size_pt"
            smallest = min(style.size_pt, style.min_size_pt)
            if smallest < floor - 0.01:
                raise ValueError(
                    f"Paragraph style '{style.id}' can be set at {smallest:.1f} pt, "
                    f"below this template's {named} of {floor:.1f} pt"
                )
        return self

    # ------------------------------------------------------------- adapting
    def adapted_to(self, geometry: dict[str, Any]) -> TemplateSpec:
        """A copy of this template laid out for a document that already exists.

        A document the operator set up by hand rarely matches the template to
        the millimetre. Rather than refuse it, the page setup is taken from
        that document and the master furniture is scaled to the new sheet, so
        the masthead still spans the page and the folio still sits at its
        foot. Everything that is not geometry - styles, colours, fonts, rules,
        PDF presets - is the template's own.
        """
        width = float(geometry.get("width_mm") or self.page_width_mm)
        height = float(geometry.get("height_mm") or self.page_height_mm)
        if width <= 0 or height <= 0:
            raise TemplateError(
                "The open document reports a page with no size",
                context={"width_mm": width, "height_mm": height},
            )

        adapted = self.model_copy(deep=True)
        scale_x = width / self.page_width_mm
        scale_y = height / self.page_height_mm
        adapted.page_width_mm = width
        adapted.page_height_mm = height
        if geometry.get("facing_pages") is not None:
            adapted.facing_pages = bool(geometry["facing_pages"])
        bleed = geometry.get("bleed_mm")
        if bleed is not None and float(bleed) >= 0:
            adapted.bleed_mm = min(20.0, float(bleed))

        margins = geometry.get("margins") or {}
        if margins:
            adapted.margins = MarginSpec(
                top=float(margins.get("top", self.margins.top)),
                bottom=float(margins.get("bottom", self.margins.bottom)),
                inside=float(margins.get("inside", self.margins.inside)),
                outside=float(margins.get("outside", self.margins.outside)),
            )
        columns = int(geometry.get("columns") or 0)
        if columns > 0:
            adapted.grid = adapted.grid.model_copy(update={"columns": columns})
        gutter = geometry.get("gutter_mm")
        if gutter is not None and float(gutter) > 0:
            adapted.grid = adapted.grid.model_copy(update={"gutter_mm": float(gutter)})

        for master in adapted.master_pages:
            for element in master.elements:
                element.x_mm *= scale_x
                element.y_mm *= scale_y
                element.width_mm *= scale_x
                element.height_mm *= scale_y
        adapted.masthead_height_mm *= scale_y
        adapted.meta = dict(adapted.meta)
        adapted.meta["adapted_from_document"] = geometry.get("name") or "open document"
        return adapted

    def geometry_matches(self, geometry: dict[str, Any], tolerance_mm: float = 1.0) -> bool:
        """Whether *geometry* is the same sheet this template describes."""
        width = float(geometry.get("width_mm") or 0)
        height = float(geometry.get("height_mm") or 0)
        return (
            abs(width - self.page_width_mm) <= tolerance_mm
            and abs(height - self.page_height_mm) <= tolerance_mm
        )

    # --------------------------------------------------------------- access
    @property
    def content_width_mm(self) -> float:
        """Width of the live area."""
        return self.page_width_mm - self.margins.inside - self.margins.outside

    @property
    def content_height_mm(self) -> float:
        """Height of the live area."""
        return self.page_height_mm - self.margins.top - self.margins.bottom

    def column_width_mm(self) -> float:
        """Width of a single grid column."""
        columns = self.grid.columns
        return (self.content_width_mm - self.grid.gutter_mm * (columns - 1)) / columns

    def paragraph_style(self, style_id: str) -> ParagraphStyleSpec | None:
        """Look up a paragraph style by id."""
        return next((s for s in self.paragraph_styles if s.id == style_id), None)

    def style_for(self, element_type: ElementType) -> ParagraphStyleSpec:
        """Paragraph style used for *element_type*, falling back to ``body``."""
        return (
            self.paragraph_style(element_type.value)
            or self.paragraph_style("body")
            or ParagraphStyleSpec(id="body", style_name="Body")
        )

    def color(self, name: str) -> ColorSpec | None:
        """Look up a colour by name."""
        return next((c for c in self.colors if c.name == name), None)

    def object_style(self, style_id: str) -> ObjectStyleSpec | None:
        """Look up an object style by id."""
        return next((s for s in self.object_styles if s.id == style_id), None)

    def master_for(self, page_index: int) -> MasterPageSpec | None:
        """Master page that applies to a 1-based page index."""
        for master in self.master_pages:
            if master.applies_to == "first" and page_index == 1:
                return master
        for master in self.master_pages:
            if master.applies_to == "all":
                return master
            if master.applies_to == "odd" and page_index % 2 == 1:
                return master
            if master.applies_to == "even" and page_index % 2 == 0:
                return master
        return self.master_pages[0] if self.master_pages else None

    def pdf_preset(self, preset_id: str) -> PDFPresetSpec | None:
        """Look up a PDF preset by id."""
        return next((p for p in self.pdf_presets if p.id == preset_id), None)

    def margins_for(self, page_index: int) -> tuple[float, float, float, float]:
        """``(top, bottom, left, right)`` margins for a page.

        On facing-page documents the *inside* margin sits on the right for
        left-hand (even) pages and on the left for right-hand (odd) pages; for
        RTL publications the binding edge is mirrored.
        """
        if not self.facing_pages:
            return (self.margins.top, self.margins.bottom, self.margins.inside, self.margins.outside)
        odd = page_index % 2 == 1
        binding_left = odd if self.direction == "rtl" else not odd
        if binding_left:
            return (self.margins.top, self.margins.bottom, self.margins.inside, self.margins.outside)
        return (self.margins.top, self.margins.bottom, self.margins.outside, self.margins.inside)

    # ------------------------------------------------------------ file I/O
    def save(self, path: Path | str) -> Path:
        """Write the template as pretty JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> TemplateSpec:
        """Read a ``*.template.json`` file."""
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def summary(self) -> dict[str, Any]:
        """Compact description shown in the Templates page."""
        return {
            "id": self.id,
            "name": self.name,
            "product_type": self.product_type,
            "language": self.language,
            "direction": self.direction,
            "page": f"{self.page_width_mm:.0f} x {self.page_height_mm:.0f} mm",
            "columns": self.grid.columns,
            "styles": len(self.paragraph_styles),
            "masters": len(self.master_pages),
            "version": self.version,
        }
