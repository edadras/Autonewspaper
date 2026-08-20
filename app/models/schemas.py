"""Data-transfer objects shared between layers.

These pydantic models are the contract between the AI layer, the layout
engine, the Adobe controllers and the UI. They are deliberately free of any
ORM or Qt dependency so they can be serialised to disk (``layout_plan.json``)
and sent to a model as JSON.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------- enums ----


class ElementType(str, Enum):
    """Kinds of frame the layout engine can place."""

    HEADLINE = "headline"
    SUBHEADLINE = "subheadline"
    LEAD = "lead"
    BODY = "body"
    IMAGE = "image"
    CAPTION = "caption"
    QUOTE = "quote"
    BYLINE = "byline"
    KICKER = "kicker"
    RULE = "rule"
    MASTHEAD = "masthead"
    FOLIO = "folio"
    ADVERTISEMENT = "advertisement"
    LOGO = "logo"
    SIDEBAR = "sidebar"


TEXT_ELEMENTS = {
    ElementType.HEADLINE,
    ElementType.SUBHEADLINE,
    ElementType.LEAD,
    ElementType.BODY,
    ElementType.CAPTION,
    ElementType.QUOTE,
    ElementType.BYLINE,
    ElementType.KICKER,
    ElementType.MASTHEAD,
    ElementType.FOLIO,
    ElementType.SIDEBAR,
}

IMAGE_ELEMENTS = {ElementType.IMAGE, ElementType.LOGO, ElementType.ADVERTISEMENT}


class AreaKind(str, Enum):
    """Editorial weight of a slot on the page."""

    MAIN = "main"
    SECONDARY = "secondary"
    SMALL = "small"
    SIDEBAR = "sidebar"


class IssueType(str, Enum):
    """Problems QA can report."""

    OVERLAP = "overlap"
    TEXT_OVERFLOW = "text_overflow"
    OUT_OF_BOUNDS = "out_of_bounds"
    MARGIN_VIOLATION = "margin_violation"
    EXCESSIVE_WHITESPACE = "excessive_whitespace"
    LOW_IMAGE_RESOLUTION = "low_image_resolution"
    SMALL_FONT = "small_font"
    POOR_HIERARCHY = "poor_hierarchy"
    MISALIGNMENT = "misalignment"
    UNBALANCED = "unbalanced"
    RTL_PROBLEM = "rtl_problem"
    EMPTY_FRAME = "empty_frame"
    IMAGE_MISSING = "image_missing"
    LOW_READABILITY = "low_readability"


class Severity(str, Enum):
    """How urgently an issue must be corrected."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PipelineStage(str, Enum):
    """Ordered stages of the autonomous pipeline."""

    IMPORT = "import"
    ANALYZE = "analyze"
    EDITORIAL = "editorial"
    ASSETS = "assets"
    IMAGE_GENERATION = "image_generation"
    IMAGE_PROCESSING = "image_processing"
    LAYOUT = "layout"
    SCORING = "scoring"
    INDESIGN = "indesign"
    RENDER = "render"
    QA = "qa"
    CORRECTION = "correction"
    EXPORT = "export"
    ARCHIVE = "archive"
    DONE = "done"

    @classmethod
    def ordered(cls) -> list[PipelineStage]:
        """Stages in execution order."""
        return [
            cls.IMPORT,
            cls.ANALYZE,
            cls.EDITORIAL,
            cls.ASSETS,
            cls.IMAGE_GENERATION,
            cls.IMAGE_PROCESSING,
            cls.LAYOUT,
            cls.SCORING,
            cls.INDESIGN,
            cls.RENDER,
            cls.QA,
            cls.CORRECTION,
            cls.EXPORT,
            cls.ARCHIVE,
            cls.DONE,
        ]


# ------------------------------------------------------------ geometry ----


class Rect(BaseModel):
    """Axis-aligned rectangle in millimetres, origin at the top-left corner."""

    model_config = ConfigDict(frozen=False)

    x: float
    y: float
    width: float
    height: float

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
        """Surface in mm²."""
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        """Centre point."""
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def aspect(self) -> float:
        """Width / height (``0`` for degenerate rectangles)."""
        return self.width / self.height if self.height else 0.0

    def intersection(self, other: Rect) -> Rect:
        """Overlapping region with *other* (zero-sized when disjoint)."""
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        return Rect(x=x, y=y, width=max(0.0, right - x), height=max(0.0, bottom - y))

    def overlaps(self, other: Rect, tolerance: float = 0.2) -> bool:
        """Whether the rectangles overlap by more than *tolerance* mm."""
        inter = self.intersection(other)
        return inter.width > tolerance and inter.height > tolerance

    def contains(self, other: Rect, tolerance: float = 0.2) -> bool:
        """Whether *other* lies fully inside this rectangle."""
        return (
            other.x >= self.x - tolerance
            and other.y >= self.y - tolerance
            and other.right <= self.right + tolerance
            and other.bottom <= self.bottom + tolerance
        )

    def inset(self, amount: float) -> Rect:
        """Shrink the rectangle by *amount* on every side."""
        return Rect(
            x=self.x + amount,
            y=self.y + amount,
            width=max(0.0, self.width - 2 * amount),
            height=max(0.0, self.height - 2 * amount),
        )

    def to_tuple(self) -> tuple[float, float, float, float]:
        """``(x, y, width, height)``."""
        return (self.x, self.y, self.width, self.height)

    def to_indesign_bounds(self) -> list[float]:
        """InDesign geometric bounds ``[y1, x1, y2, x2]`` in millimetres."""
        return [self.y, self.x, self.bottom, self.right]


# ---------------------------------------------------------- typography ----


class TypographySpec(BaseModel):
    """Resolved typography for one frame."""

    style_name: str = "Body"
    font_family: str = "IRANSans"
    font_style: str = "Regular"
    size_pt: float = 9.5
    leading_pt: float = 12.5
    tracking: float = 0.0
    alignment: Literal["left", "right", "center", "justify", "justify_last_right"] = "justify"
    direction: Literal["rtl", "ltr"] = "rtl"
    color: str = "Black"
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0
    hyphenation: bool = False
    columns: int = 1
    column_gutter_mm: float = 4.0
    all_caps: bool = False
    fallback_fonts: list[str] = Field(default_factory=list)

    def scaled(self, factor: float) -> TypographySpec:
        """Return a copy with the size and leading scaled by *factor*."""
        data = self.model_dump()
        data["size_pt"] = round(self.size_pt * factor, 2)
        data["leading_pt"] = round(self.leading_pt * factor, 2)
        return TypographySpec(**data)


# -------------------------------------------------------------- layout ----


class ElementSpec(BaseModel):
    """One frame in a layout plan."""

    id: str
    type: ElementType
    rect: Rect
    z_index: int = 0
    article_id: int | None = None
    asset_id: int | None = None
    text: str = ""
    image_path: str | None = None
    style_id: str = ""
    typography: TypographySpec | None = None
    column_span: int = 1
    rotation: float = 0.0
    locked: bool = False
    fit_mode: Literal["fill", "fit", "proportional", "none"] = "fill"
    frame_name: str = ""
    stroke_weight_pt: float = 0.0
    fill_color: str | None = None
    text_wrap_mm: float = 0.0
    estimated_overflow: float = 0.0
    """Fraction of text that does not fit (``0`` = fits, ``0.2`` = 20% too much)."""
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        """Whether this element carries text."""
        return self.type in TEXT_ELEMENTS

    @property
    def is_image(self) -> bool:
        """Whether this element carries an image."""
        return self.type in IMAGE_ELEMENTS

    @field_validator("frame_name")
    @classmethod
    def _default_frame_name(cls, value: str, info: Any) -> str:
        return value or ""

    @model_validator(mode="after")
    def _fill_frame_name(self) -> ElementSpec:
        if not self.frame_name:
            self.frame_name = f"{self.type.value}_{self.id}"
        return self


class PageLayout(BaseModel):
    """The full plan for one page."""

    index: int
    width_mm: float
    height_mm: float
    margin_top_mm: float = 15.0
    margin_bottom_mm: float = 15.0
    margin_inside_mm: float = 15.0
    margin_outside_mm: float = 12.0
    bleed_mm: float = 3.0
    columns: int = 6
    gutter_mm: float = 4.0
    section: str = ""
    master: str = "A-Master"
    elements: list[ElementSpec] = Field(default_factory=list)
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    candidate_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_rect(self) -> Rect:
        """Live area of the page (inside the margins)."""
        return Rect(
            x=self.margin_inside_mm,
            y=self.margin_top_mm,
            width=self.width_mm - self.margin_inside_mm - self.margin_outside_mm,
            height=self.height_mm - self.margin_top_mm - self.margin_bottom_mm,
        )

    @property
    def page_rect(self) -> Rect:
        """The trim box."""
        return Rect(x=0, y=0, width=self.width_mm, height=self.height_mm)

    def column_width(self) -> float:
        """Width of a single column."""
        content = self.content_rect
        if self.columns <= 0:
            return content.width
        return (content.width - self.gutter_mm * (self.columns - 1)) / self.columns

    def column_x(self, column: int) -> float:
        """Left edge of *column* (0-based, left-to-right)."""
        return self.content_rect.x + column * (self.column_width() + self.gutter_mm)

    def span_width(self, span: int) -> float:
        """Width covered by *span* adjacent columns."""
        span = max(1, min(span, self.columns))
        return self.column_width() * span + self.gutter_mm * (span - 1)

    def element(self, element_id: str) -> ElementSpec | None:
        """Look up an element by id."""
        return next((e for e in self.elements if e.id == element_id), None)

    def articles(self) -> list[int]:
        """Distinct article ids present on this page."""
        seen: list[int] = []
        for element in self.elements:
            if element.article_id is not None and element.article_id not in seen:
                seen.append(element.article_id)
        return seen

    def ink_area(self) -> float:
        """Total area covered by elements (overlaps counted once each)."""
        return sum(e.rect.area for e in self.elements)


class LayoutPlan(BaseModel):
    """The layout of a whole edition."""

    project_id: int
    project_slug: str = ""
    template_id: str = ""
    design_style: str = "classic"
    language: str = "fa"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pages: list[PageLayout] = Field(default_factory=list)
    score: float = 0.0
    iteration: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)

    def page(self, index: int) -> PageLayout | None:
        """Look up a page by its 1-based index."""
        return next((p for p in self.pages if p.index == index), None)

    def element_count(self) -> int:
        """Total number of frames across the edition."""
        return sum(len(p.elements) for p in self.pages)

    def save(self, path: Path | str) -> Path:
        """Write the plan to *path* as pretty JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> LayoutPlan:
        """Read a plan previously written with :meth:`save`."""
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


# ----------------------------------------------------------- editorial ----


class ArticleAnalysis(BaseModel):
    """Editorial scoring of a single article."""

    article_id: int
    importance: int = Field(50, ge=0, le=100)
    urgency: int = Field(50, ge=0, le=100)
    public_interest: int = Field(50, ge=0, le=100)
    visual_importance: int = Field(50, ge=0, le=100)
    category: str = "general"
    recommended_page: int = 1
    recommended_area: AreaKind = AreaKind.SECONDARY
    headline: str = ""
    subtitle: str = ""
    lead: str = ""
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    image_required: bool = False
    ai_image_prompt: str = ""
    rationale: str = ""

    @property
    def priority(self) -> int:
        """Composite priority used for slot assignment."""
        return int(
            round(
                0.40 * self.importance
                + 0.25 * self.urgency
                + 0.25 * self.public_interest
                + 0.10 * self.visual_importance
            )
        )


class EditorialPlan(BaseModel):
    """Output of the editorial agent for the whole edition."""

    project_id: int
    analyses: list[ArticleAnalysis] = Field(default_factory=list)
    page_assignments: dict[int, list[int]] = Field(default_factory=dict)
    """``page index -> [article_id, ...]`` ordered by descending priority."""
    front_page_lead: int | None = None
    notes: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def analysis_for(self, article_id: int) -> ArticleAnalysis | None:
        """Look up the analysis of one article."""
        return next((a for a in self.analyses if a.article_id == article_id), None)

    def save(self, path: Path | str) -> Path:
        """Persist the plan as JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> EditorialPlan:
        """Read a plan written by :meth:`save`."""
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


# ------------------------------------------------------------------ QA ----


class QAIssue(BaseModel):
    """A single problem found on a rendered page."""

    type: IssueType
    severity: Severity = Severity.MEDIUM
    element_id: str | None = None
    page_index: int = 1
    message: str = ""
    rect: Rect | None = None
    suggestion: str = ""
    detected_by: Literal["geometry", "pixels", "vision_ai", "indesign"] = "geometry"

    @property
    def weight(self) -> float:
        """Penalty weight used when computing the QA score."""
        return {
            Severity.LOW: 1.0,
            Severity.MEDIUM: 3.0,
            Severity.HIGH: 7.0,
            Severity.CRITICAL: 15.0,
        }[self.severity]


class QAReport(BaseModel):
    """Quality assessment of one page."""

    page_index: int
    score: float = 0.0
    issues: list[QAIssue] = Field(default_factory=list)
    preview_path: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    analyzed_by: list[str] = Field(default_factory=list)
    iteration: int = 0

    @property
    def blocking_issues(self) -> list[QAIssue]:
        """Issues that must be fixed before the page can be accepted."""
        return [i for i in self.issues if i.severity in (Severity.HIGH, Severity.CRITICAL)]

    def passed(self, threshold: float) -> bool:
        """Whether the page meets *threshold* and has no blocking issue."""
        return self.score >= threshold and not self.blocking_issues


class EditionQAReport(BaseModel):
    """QA across every page of the edition."""

    project_id: int
    pages: list[QAReport] = Field(default_factory=list)
    iteration: int = 0

    @property
    def score(self) -> float:
        """Mean page score."""
        if not self.pages:
            return 0.0
        return round(sum(p.score for p in self.pages) / len(self.pages), 2)

    def failing(self, threshold: float) -> list[QAReport]:
        """Pages that did not pass QA."""
        return [p for p in self.pages if not p.passed(threshold)]


# ------------------------------------------------------------- imaging ----


class ImageRequest(BaseModel):
    """A request handed to an image-generation provider."""

    subject: str
    kind: str = "News Photography"
    style: str = "Realistic Editorial Photography"
    aspect_ratio: str = "16:9"
    width_px: int = 1536
    height_px: int = 864
    negative_prompt: str = "no text, no watermark, no logo"
    language: str = "fa"
    article_id: int | None = None
    seed: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_prompt(self) -> str:
        """Render the request as a provider prompt."""
        parts = [
            f"{self.kind}: {self.subject}",
            f"Style: {self.style}",
            f"Aspect ratio: {self.aspect_ratio}",
            "Composition: editorial press photograph, natural lighting, documentary framing",
            f"Avoid: {self.negative_prompt}",
        ]
        return "\n".join(parts)


class GeneratedImage(BaseModel):
    """Result of an image generation call."""

    path: str
    provider: str
    model: str
    prompt: str
    width: int
    height: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    seed: int | None = None
    revised_prompt: str = ""

    def metadata(self) -> dict[str, Any]:
        """Provenance metadata stored next to the asset (see §40)."""
        return {
            "ai_generated": True,
            "provider": self.provider,
            "model": self.model,
            "prompt": self.prompt,
            "revised_prompt": self.revised_prompt,
            "seed": self.seed,
            "timestamp": self.created_at.isoformat(),
        }


class ImageAnalysis(BaseModel):
    """Result of the image intelligence pass (see §39)."""

    path: str
    width: int = 0
    height: int = 0
    aspect_ratio: float = 1.0
    dpi: float = 72.0
    orientation: Literal["landscape", "portrait", "square"] = "landscape"
    sharpness: float = 0.0
    """Variance-of-Laplacian style score; higher is sharper."""
    brightness: float = 0.0
    contrast: float = 0.0
    colorfulness: float = 0.0
    face_count: int = 0
    quality_score: float = 0.0
    checksum: str = ""
    duplicate_of: str | None = None
    problems: list[str] = Field(default_factory=list)

    def usable_as_main(self, min_width: int = 1200, min_quality: float = 55.0) -> bool:
        """Whether the image is good enough for a lead position."""
        return self.width >= min_width and self.quality_score >= min_quality and not self.problems


# ------------------------------------------------------------ pipeline ----


class ApprovalRequest(BaseModel):
    """A pause point in semi-automatic mode."""

    stage: PipelineStage
    title: str
    description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class PipelineResult(BaseModel):
    """Everything a completed run produced."""

    project_id: int
    success: bool = False
    stage_reached: PipelineStage = PipelineStage.IMPORT
    iterations: int = 0
    score: float = 0.0
    pdf_paths: list[str] = Field(default_factory=list)
    indd_path: str | None = None
    idml_path: str | None = None
    preview_paths: list[str] = Field(default_factory=list)
    archive_path: str | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float = 0.0
    adobe_strategy: str = ""

    def summary(self) -> str:
        """One-line summary for logs and notifications."""
        state = "completed" if self.success else "incomplete"
        return (
            f"Pipeline {state} at stage '{self.stage_reached.value}' "
            f"after {self.iterations} iteration(s), score {self.score:.1f}"
        )


class ProjectSpec(BaseModel):
    """Parameters of the New Project wizard."""

    name: str
    publication_name: str = ""
    edition_date: date = Field(default_factory=date.today)
    language: Literal["fa", "en", "ar", "tr"] = "fa"
    product_type: Literal[
        "newspaper", "magazine", "brochure", "catalog", "flyer", "poster", "digital"
    ] = "newspaper"
    page_size: str = "Broadsheet"
    page_width_mm: float = 297.0
    page_height_mm: float = 420.0
    page_count: int = Field(8, ge=1, le=200)
    template_id: str = "broadsheet_fa_standard"
    design_style: str = "classic"
    ai_provider: str = "heuristic"

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Project name must not be empty")
        return value.strip()
