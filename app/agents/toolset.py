"""The concrete tools an agent may call.

Each entry corresponds to one line of specification §56. The tools operate on
the layout plan and - when InDesign is reachable - on the live document, so a
change an agent makes is real, verifiable and undoable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
    verify_geometry,
    verify_truthy,
)
from app.core.errors import ToolValidationError
from app.core.events import EventBus
from app.core.undo import Command, UndoStack
from app.export.exporter import ExportService
from app.layout.engine import LayoutEngine
from app.models.schemas import ElementSpec, ElementType, LayoutPlan, PageLayout, Rect
from app.services.asset_manager import AssetManager
from app.services.project_manager import ProjectHandle
from app.templates.schema import TemplateSpec
from app.vision.qa_agent import VisionQAAgent
from app.vision.renderer import PreviewRenderer

log = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """Everything the tools need in order to do real work."""

    handle: ProjectHandle
    template: TemplateSpec
    plan: LayoutPlan
    engine: LayoutEngine
    adobe: AdobeService
    assets: AssetManager
    exporter: ExportService
    qa: VisionQAAgent
    renderer: PreviewRenderer
    undo: UndoStack | None = None
    bus: EventBus | None = None
    preview_dpi: int = 110
    notes: list[str] = field(default_factory=list)

    # -- lookup ----------------------------------------------------------
    def page(self, index: int) -> PageLayout:
        """Return a page or raise a validation error the agent can act on."""
        page = self.plan.page(index)
        if page is None:
            raise ToolValidationError(
                f"Page {index} does not exist",
                context={"pages": [p.index for p in self.plan.pages]},
            )
        return page

    def element(self, element_id: str) -> tuple[PageLayout, ElementSpec]:
        """Find an element and the page it belongs to."""
        for page in self.plan.pages:
            element = page.element(element_id)
            if element is not None:
                return (page, element)
        raise ToolValidationError(
            f"Element '{element_id}' does not exist",
            context={"pages": [p.index for p in self.plan.pages]},
        )

    @property
    def indesign_live(self) -> bool:
        """Whether the InDesign document can be driven right now."""
        return self.adobe.indesign_app.installed and self.adobe.indesign.available()

    def record(self, label: str, undo: Callable[[], Any]) -> None:
        """Record an undo step for a change a tool has already applied."""
        if self.undo is None:
            return
        self.undo.push(Command(label=label, do=lambda: None, undo=undo), execute=False)


def build_toolset(
    context: AgentContext,
    policy: PermissionPolicy | None = None,
    bus: EventBus | None = None,
) -> ToolRegistry:
    """Register every agent tool against *context*."""
    registry = ToolRegistry(policy or PermissionPolicy(), bus or context.bus)

    # ----------------------------------------------------------- sessions
    registry.register(
        Tool(
            name="open_indesign",
            description="Connect to Adobe InDesign, launching it if necessary.",
            parameters=[],
            capability="adobe.launch",
            handler=lambda: _open_host(context, "indesign"),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="open_photoshop",
            description="Connect to Adobe Photoshop, launching it if necessary.",
            parameters=[],
            capability="adobe.launch",
            handler=lambda: _open_host(context, "photoshop"),
            verifier=verify_truthy,
        )
    )

    # -------------------------------------------------------------- pages
    registry.register(
        Tool(
            name="create_page",
            description="Add a page to the edition at the given 1-based index.",
            parameters=[
                Parameter("index", "integer", "Position of the new page", minimum=1, maximum=200),
                Parameter("section", "string", "Section name", required=False, default=""),
            ],
            capability="layout.write",
            handler=lambda index, section="": _create_page(context, index, section),
            verifier=verify_truthy,
        )
    )

    # ------------------------------------------------------------- frames
    geometry_params = [
        Parameter("page", "integer", "Page index", minimum=1, maximum=200),
        Parameter("x_mm", "number", "Left edge in millimetres", minimum=0, maximum=2000),
        Parameter("y_mm", "number", "Top edge in millimetres", minimum=0, maximum=2000),
        Parameter("width_mm", "number", "Frame width", minimum=5, maximum=2000),
        Parameter("height_mm", "number", "Frame height", minimum=5, maximum=2000),
    ]
    registry.register(
        Tool(
            name="create_text_frame",
            description="Create a text frame and fill it with copy.",
            parameters=[
                *geometry_params,
                Parameter("text", "string", "Text to place in the frame"),
                Parameter(
                    "style",
                    "string",
                    "Paragraph style",
                    required=False,
                    default="body",
                    choices=[t.value for t in ElementType],
                ),
                Parameter("columns", "integer", "Text columns", required=False, default=1,
                          minimum=1, maximum=8),
            ],
            capability="layout.write",
            handler=lambda page, x_mm, y_mm, width_mm, height_mm, text, style="body", columns=1: (
                _create_frame(context, page, x_mm, y_mm, width_mm, height_mm,
                              kind=style, text=text, columns=columns)
            ),
            verifier=verify_geometry,
        )
    )
    registry.register(
        Tool(
            name="create_image_frame",
            description="Create a picture frame, optionally placing an image in it.",
            parameters=[
                *geometry_params,
                Parameter("image_path", "string", "Image file to place", required=False, default=""),
                Parameter("asset_id", "integer", "Asset id", required=False, minimum=1),
            ],
            capability="layout.write",
            handler=lambda page, x_mm, y_mm, width_mm, height_mm, image_path="", asset_id=None: (
                _create_frame(context, page, x_mm, y_mm, width_mm, height_mm,
                              kind="image", image_path=image_path, asset_id=asset_id)
            ),
            verifier=verify_geometry,
        )
    )
    registry.register(
        Tool(
            name="place_image",
            description="Place an image into an existing picture frame.",
            parameters=[
                Parameter("element_id", "string", "Frame id"),
                Parameter("image_path", "string", "Image file"),
                Parameter("fit_mode", "string", "How to fit the image", required=False,
                          default="fill", choices=["fill", "fit", "proportional", "none"]),
            ],
            capability="layout.write",
            handler=lambda element_id, image_path, fit_mode="fill": _place_image(
                context, element_id, image_path, fit_mode
            ),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="insert_text",
            description="Replace the copy of a text frame.",
            parameters=[
                Parameter("element_id", "string", "Frame id"),
                Parameter("text", "string", "New text"),
            ],
            capability="layout.write",
            handler=lambda element_id, text: _insert_text(context, element_id, text),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="resize_element",
            description="Set the size of a frame in millimetres.",
            parameters=[
                Parameter("element_id", "string", "Frame id"),
                Parameter("width_mm", "number", "New width", minimum=5, maximum=2000),
                Parameter("height_mm", "number", "New height", minimum=5, maximum=2000),
            ],
            capability="layout.write",
            handler=lambda element_id, width_mm, height_mm: _resize(
                context, element_id, width_mm, height_mm
            ),
            verifier=verify_geometry,
        )
    )
    registry.register(
        Tool(
            name="move_element",
            description="Move a frame to an absolute position in millimetres.",
            parameters=[
                Parameter("element_id", "string", "Frame id"),
                Parameter("x_mm", "number", "New left edge", minimum=0, maximum=2000),
                Parameter("y_mm", "number", "New top edge", minimum=0, maximum=2000),
            ],
            capability="layout.write",
            handler=lambda element_id, x_mm, y_mm: _move(context, element_id, x_mm, y_mm),
            verifier=verify_geometry,
        )
    )
    registry.register(
        Tool(
            name="apply_style",
            description="Apply a paragraph style to a text frame.",
            parameters=[
                Parameter("element_id", "string", "Frame id"),
                Parameter("style", "string", "Style id",
                          choices=[t.value for t in ElementType]),
            ],
            capability="layout.write",
            handler=lambda element_id, style: _apply_style(context, element_id, style),
            verifier=verify_truthy,
        )
    )

    # ---------------------------------------------------------- rendering
    registry.register(
        Tool(
            name="render_page",
            description="Render a page to an image for review.",
            parameters=[Parameter("page", "integer", "Page index", minimum=1, maximum=200)],
            capability="layout.read",
            handler=lambda page: _render_page(context, page),
            verifier=verify_exists,
        )
    )
    registry.register(
        Tool(
            name="analyze_page",
            description="Run quality assurance on a page and report its issues.",
            parameters=[Parameter("page", "integer", "Page index", minimum=1, maximum=200)],
            capability="layout.read",
            handler=lambda page: _analyze_page(context, page),
            verifier=verify_truthy,
        )
    )

    # ------------------------------------------------------------- assets
    registry.register(
        Tool(
            name="generate_image",
            description="Generate a picture for a story with the configured image provider.",
            parameters=[
                Parameter("article_id", "integer", "Story id", minimum=1),
                Parameter("subject", "string", "What the picture must show"),
                Parameter("aspect_ratio", "string", "Aspect ratio", required=False,
                          default="16:9", choices=["16:9", "4:3", "3:2", "1:1", "3:4", "2:3"]),
                Parameter("category", "string", "Story category", required=False, default="general"),
            ],
            capability="ai.generate_image",
            handler=lambda article_id, subject, aspect_ratio="16:9", category="general": (
                _generate_image(context, article_id, subject, aspect_ratio, category)
            ),
            verifier=verify_truthy,
        )
    )
    registry.register(
        Tool(
            name="process_image",
            description="Crop and resample a picture for the frame it will occupy.",
            parameters=[
                Parameter("asset_id", "integer", "Asset id", minimum=1),
                Parameter("width_mm", "number", "Frame width", minimum=5, maximum=2000),
                Parameter("height_mm", "number", "Frame height", minimum=5, maximum=2000),
            ],
            capability="assets.write",
            handler=lambda asset_id, width_mm, height_mm: context.assets.process_for_frame(
                context.handle, asset_id, frame_width_mm=width_mm, frame_height_mm=height_mm
            ),
            verifier=verify_truthy,
        )
    )

    # ------------------------------------------------------------- output
    registry.register(
        Tool(
            name="export_pdf",
            description="Export the edition to PDF with the given preset.",
            parameters=[
                Parameter("preset", "string", "PDF preset", required=False, default="print",
                          choices=["print", "high_quality", "digital", "web"]),
            ],
            capability="export.write",
            handler=lambda preset="print": _export_pdf(context, preset),
            verifier=verify_exists,
        )
    )
    registry.register(
        Tool(
            name="save_project",
            description="Persist the layout plan and the project manifest.",
            parameters=[],
            capability="project.write",
            handler=lambda: _save_project(context),
            verifier=verify_exists,
        )
    )
    return registry


# ------------------------------------------------------------ handlers ----


def _open_host(context: AgentContext, host: str) -> dict[str, Any]:
    controller = context.adobe.indesign if host == "indesign" else context.adobe.photoshop
    if not (context.adobe.indesign_app if host == "indesign" else context.adobe.photoshop_app).installed:
        return {"connected": False, "reason": f"{host} is not installed on this machine"}
    strategy = controller.connect()
    return {"connected": True, "strategy": strategy, "host": host}


def _create_page(context: AgentContext, index: int, section: str) -> dict[str, Any]:
    if context.plan.page(index) is not None:
        return {"created": False, "reason": f"page {index} already exists"}
    template = context.template
    top, bottom, left, right = template.margins_for(index)
    page = PageLayout(
        index=index,
        width_mm=template.page_width_mm,
        height_mm=template.page_height_mm,
        margin_top_mm=top,
        margin_bottom_mm=bottom,
        margin_inside_mm=left,
        margin_outside_mm=right,
        bleed_mm=template.bleed_mm,
        columns=template.grid.columns,
        gutter_mm=template.grid.gutter_mm,
        section=section,
        master=(template.master_for(index).name if template.master_for(index) else "A-Master"),
    )
    context.plan.pages.append(page)
    context.plan.pages.sort(key=lambda p: p.index)
    if context.indesign_live:
        context.adobe.indesign.ensure_pages(len(context.plan.pages))
    return {"created": True, "page": index}


def _create_frame(
    context: AgentContext,
    page_index: int,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    *,
    kind: str,
    text: str = "",
    image_path: str = "",
    asset_id: int | None = None,
    columns: int = 1,
) -> dict[str, Any]:
    page = context.page(page_index)
    rect = Rect(x=x_mm, y=y_mm, width=width_mm, height=height_mm)
    if not page.page_rect.contains(rect, tolerance=0.5):
        raise ToolValidationError(
            f"The frame would fall outside page {page_index} "
            f"({page.width_mm:.0f}x{page.height_mm:.0f} mm)"
        )
    for other in page.elements:
        if other.rect.overlaps(rect, tolerance=0.6):
            raise ToolValidationError(
                f"The frame would overlap '{other.frame_name}'; move or resize it first"
            )

    element_type = ElementType(kind) if kind in {t.value for t in ElementType} else ElementType.BODY
    element_id = f"agent_{page_index}_{len(page.elements) + 1}"
    element = ElementSpec(
        id=element_id,
        type=element_type,
        rect=rect,
        z_index=max((e.z_index for e in page.elements), default=0) + 1,
        text=text,
        image_path=image_path or None,
        asset_id=asset_id,
        style_id=element_type.value,
        column_span=columns,
    )
    if element.is_text and text:
        fit = context.engine.typography.fit(
            text, rect, element_type, columns=columns, allow_truncate=False
        )
        element.typography = fit.typography
        element.estimated_overflow = fit.overflow
    page.elements.append(element)

    if context.indesign_live:
        controller = context.adobe.indesign
        if element.is_image:
            controller.create_image_frame(page_index, element, context.template)
        else:
            controller.create_text_frame(page_index, element, context.template)
    return {
        "id": element_id,
        "x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height,
        "overflow": element.estimated_overflow,
    }


def _place_image(
    context: AgentContext, element_id: str, image_path: str, fit_mode: str
) -> dict[str, Any]:
    _page, element = context.element(element_id)
    if not element.is_image:
        raise ToolValidationError(f"'{element_id}' is not a picture frame")
    path = Path(image_path)
    if not path.exists():
        raise ToolValidationError(f"The image {image_path} does not exist")
    element.image_path = str(path)
    element.fit_mode = fit_mode  # type: ignore[assignment]
    if context.indesign_live:
        context.adobe.indesign.place_image(element.frame_name, path, fit_mode)
    return {"id": element_id, "image": str(path), "fit_mode": fit_mode}


def _insert_text(context: AgentContext, element_id: str, text: str) -> dict[str, Any]:
    _page, element = context.element(element_id)
    if not element.is_text:
        raise ToolValidationError(f"'{element_id}' is not a text frame")
    element.text = text
    fit = context.engine.typography.fit(
        text, element.rect, element.type,
        columns=max(1, element.column_span), allow_truncate=False,
    )
    element.typography = fit.typography
    element.estimated_overflow = fit.overflow
    if context.indesign_live:
        style = fit.typography.style_name
        context.adobe.indesign.insert_text(element.frame_name, text, style)
    return {"id": element_id, "characters": len(text), "overflow": fit.overflow}


def _resize(context: AgentContext, element_id: str, width_mm: float, height_mm: float) -> dict[str, Any]:
    page, element = context.element(element_id)
    candidate = Rect(x=element.rect.x, y=element.rect.y, width=width_mm, height=height_mm)
    _guard_placement(page, element, candidate)
    element.rect = candidate
    context.engine.refit(page)
    if context.indesign_live:
        context.adobe.indesign.set_bounds(element.frame_name, candidate)
    return {
        "id": element_id, "x": candidate.x, "y": candidate.y,
        "width": candidate.width, "height": candidate.height,
        "overflow": element.estimated_overflow,
    }


def _move(context: AgentContext, element_id: str, x_mm: float, y_mm: float) -> dict[str, Any]:
    page, element = context.element(element_id)
    candidate = Rect(x=x_mm, y=y_mm, width=element.rect.width, height=element.rect.height)
    _guard_placement(page, element, candidate)
    element.rect = candidate
    if context.indesign_live:
        context.adobe.indesign.set_bounds(element.frame_name, candidate)
    return {
        "id": element_id, "x": candidate.x, "y": candidate.y,
        "width": candidate.width, "height": candidate.height,
    }


def _guard_placement(page: PageLayout, element: ElementSpec, candidate: Rect) -> None:
    """Refuse a change that would break the page's hard constraints."""
    if element.locked:
        raise ToolValidationError(f"'{element.id}' is master-page furniture and is locked")
    if not page.page_rect.contains(candidate, tolerance=0.5):
        raise ToolValidationError("The change would push the frame off the page")
    for other in page.elements:
        if other.id == element.id or other.type is ElementType.RULE:
            continue
        if candidate.overlaps(other.rect, tolerance=0.6):
            raise ToolValidationError(f"The change would overlap '{other.frame_name}'")


def _apply_style(context: AgentContext, element_id: str, style: str) -> dict[str, Any]:
    _page, element = context.element(element_id)
    element_type = ElementType(style)
    element.type = element_type
    element.style_id = style
    if element.is_text:
        fit = context.engine.typography.fit(
            element.text, element.rect, element_type,
            columns=max(1, element.column_span), allow_truncate=False,
        )
        element.typography = fit.typography
        element.estimated_overflow = fit.overflow
        if context.indesign_live:
            context.adobe.indesign.apply_paragraph_style(
                element.frame_name, fit.typography.style_name
            )
    return {"id": element_id, "style": style, "overflow": element.estimated_overflow}


def _render_page(context: AgentContext, page_index: int) -> dict[str, Any]:
    page = context.page(page_index)
    target = context.handle.previews_dir / f"agent_page_{page_index:03d}.png"
    if context.indesign_live:
        try:
            context.adobe.indesign.render_preview(page_index, target, dpi=context.preview_dpi)
            return {"path": str(target), "engine": "indesign"}
        except Exception as exc:  # noqa: BLE001
            context.notes.append(f"InDesign preview failed ({exc}); the built-in renderer was used")
    result = context.renderer.render_page(page, target)
    return {"path": str(result.path), "engine": "builtin", "warnings": result.warnings}


def _analyze_page(context: AgentContext, page_index: int) -> dict[str, Any]:
    page = context.page(page_index)
    preview = _render_page(context, page_index)
    report = None
    if context.indesign_live:
        try:
            report = context.adobe.indesign.page_report(page_index)
        except Exception as exc:  # noqa: BLE001
            context.notes.append(f"InDesign page report failed: {exc}")
    qa = context.qa.analyze_page(
        page,
        preview_path=Path(preview["path"]),
        indesign_report=report,
        asset_quality=context.assets.quality_map(context.handle),
        asset_pixels=context.assets.pixel_map(context.handle),
    )
    content = page.content_rect
    return {
        "page": page_index,
        "score": qa.score,
        "threshold": context.qa.threshold,
        "passed": qa.passed(context.qa.threshold),
        "preview": preview["path"],
        "live_area": {
            "x": round(content.x, 2), "y": round(content.y, 2),
            "width": round(content.width, 2), "height": round(content.height, 2),
        },
        "issues": [
            {
                "type": issue.type.value,
                "severity": issue.severity.value,
                "element_id": issue.element_id,
                "message": issue.message,
                "suggestion": issue.suggestion,
            }
            for issue in qa.issues
        ],
        "elements": [
            {
                "id": element.id,
                "type": element.type.value,
                "x": round(element.rect.x, 2),
                "y": round(element.rect.y, 2),
                "width": round(element.rect.width, 2),
                "height": round(element.rect.height, 2),
                "overflow": round(element.estimated_overflow, 3),
                "locked": element.locked,
            }
            for element in page.elements
        ],
    }


def _generate_image(
    context: AgentContext, article_id: int, subject: str, aspect_ratio: str, category: str
) -> dict[str, Any]:
    asset_id = context.assets.generate_for_article(
        context.handle,
        article_id,
        subject=subject,
        category=category,
        design_style=context.plan.design_style,
        aspect_ratio=aspect_ratio,
        language=context.plan.language,
    )
    if asset_id is None:
        return {"generated": False, "reason": "the image provider produced nothing"}
    with context.handle.uow() as uow:
        asset = uow.assets.get(asset_id)
        return {
            "generated": True,
            "asset_id": asset_id,
            "path": asset.path if asset else "",
            "width": asset.width if asset else 0,
            "height": asset.height if asset else 0,
            "ai_generated": bool(asset.ai_generated) if asset else False,
        }


def _export_pdf(context: AgentContext, preset: str) -> dict[str, Any]:
    result = context.exporter.export(
        context.handle, context.plan, context.template, presets=[preset],
        export_indd=False, export_idml=False, export_previews=False,
    )
    path = result.pdfs.get(preset) or result.primary_pdf
    return {"path": path, "engine": result.engine, "warnings": result.warnings}


def _save_project(context: AgentContext) -> dict[str, Any]:
    path = context.plan.save(context.handle.layout_plan_path)
    return {"path": str(path), "pages": len(context.plan.pages)}
