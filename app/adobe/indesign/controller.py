"""InDesign controller.

The operation-level API the rest of the application uses. Every method turns
into one generated ExtendScript program executed through
:class:`~app.adobe.bridge.AdobeBridge`; nothing here moves the mouse.

When InDesign is not present the controller reports that plainly through
:meth:`InDesignController.available` - the pipeline then produces the layout
plan, the internal previews and a PDF from the built-in renderer, and records
in the run result that the Adobe stage was skipped.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.adobe.bridge import AdobeBridge, ScriptResult, working_directory
from app.adobe.detect import AdobeApp, detect_indesign
from app.adobe.jsx import (
    ScriptBuilder,
    build_document_script,
    build_page_script,
    build_probe_script,
    element_payload,
    typography_payload,
)
from app.core.errors import AdobeNotFoundError, ScriptExecutionError
from app.core.events import EventBus, EventType
from app.models.schemas import ElementSpec, LayoutPlan, PageLayout, Rect
from app.templates.schema import PDFPresetSpec, TemplateSpec

log = logging.getLogger(__name__)


class InDesignController:
    """Drives Adobe InDesign through its scripting API."""

    def __init__(
        self,
        work_dir: Path,
        *,
        app: AdobeApp | None = None,
        bus: EventBus | None = None,
        prefer_com: bool = True,
        allow_queue: bool = True,
        script_timeout: float = 600.0,
        launch_timeout: float = 180.0,
    ) -> None:
        self.app = app or detect_indesign()
        self.work_dir = working_directory(Path(work_dir), "indesign")
        self.bridge = AdobeBridge(
            self.app,
            self.work_dir,
            prefer_com=prefer_com,
            allow_queue=allow_queue,
            default_timeout=script_timeout,
        )
        self.bus = bus
        self.launch_timeout = launch_timeout
        self.document_path: Path | None = None
        self._connected = False

    # ---------------------------------------------------------- life-cycle
    def available(self) -> bool:
        """Whether InDesign is installed and reachable."""
        return self.app.installed

    def connect(self, launch: bool = True) -> str:
        """Connect to InDesign, launching it when necessary."""
        if not self.app.installed:
            raise AdobeNotFoundError(
                "Adobe InDesign was not found on this machine",
                context=self.app.to_dict(),
                recovery_action="Install InDesign, or set its path in Adobe Settings.",
            )
        strategy = self.bridge.connect(launch=launch, timeout=self.launch_timeout)
        self._connected = True
        self._emit(
            EventType.ADOBE_CONNECTED, host="indesign", strategy=strategy.name, version=self.app.version
        )
        return strategy.name

    def health(self) -> dict[str, Any]:
        """Probe the running application (version, presets, fonts)."""
        if not self.app.installed:
            return {"installed": False, "reason": "InDesign not detected", **self.app.to_dict()}
        try:
            self.connect()
            result = self._run(build_probe_script("indesign"), "probe")
            info = dict(result.data or {})
            info.update({"installed": True, "strategy": result.strategy})
            return info
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            return {"installed": True, "reachable": False, "error": str(exc)[:400], **self.app.to_dict()}

    def disconnect(self) -> None:
        """Release the connection."""
        self.bridge.shutdown()
        self._connected = False
        self._emit(EventType.ADOBE_DISCONNECTED, host="indesign")

    shutdown = disconnect

    # ------------------------------------------------------------ document
    def create_document(self, template: TemplateSpec, page_count: int) -> dict[str, Any]:
        """Create a blank document with the template's page geometry."""
        builder = ScriptBuilder("indesign", "create_document")
        builder.call("setup", {"enableRedraw": False})
        builder.call(
            "createDocument",
            {
                "page_width_mm": template.page_width_mm,
                "page_height_mm": template.page_height_mm,
                "facing_pages": template.facing_pages,
                "page_count": page_count,
                "bleed_mm": template.bleed_mm,
            },
            assign="__info",
        )
        builder.emit("__info")
        return self._run(builder.build(), "create_document").data or {}

    def open_template(self, path: Path | str, as_copy: bool = True) -> dict[str, Any]:
        """Open an ``.indt``/``.indd`` template."""
        builder = ScriptBuilder("indesign", "open_template")
        builder.call("setup", {"enableRedraw": False})
        builder.call("openTemplate", str(path), as_copy, assign="__info")
        builder.emit("__info")
        return self._run(builder.build(), "open_template").data or {}

    def open_document(self, path: Path | str) -> dict[str, Any]:
        """Open an existing document."""
        builder = ScriptBuilder("indesign", "open_document")
        builder.call("setup", {"enableRedraw": False})
        builder.call("openDocument", str(path), assign="__info")
        builder.emit("__info")
        result = self._run(builder.build(), "open_document")
        self.document_path = Path(path)
        return result.data or {}

    def ensure_pages(self, count: int) -> int:
        """Make the document have exactly *count* pages."""
        builder = ScriptBuilder("indesign", "ensure_pages")
        builder.call("ensurePages", count, assign="__n")
        builder.emit("__n")
        return int(self._run(builder.build(), "ensure_pages").data or 0)

    def apply_template_styles(self, template: TemplateSpec) -> bool:
        """Create the template's colours, paragraph and object styles."""
        from app.adobe.jsx import template_payload

        styles = [
            typography_payload(None, style.id) | style.model_dump() for style in template.paragraph_styles
        ]
        builder = ScriptBuilder("indesign", "apply_styles")
        builder.var("__template", template_payload(template, styles))
        builder.raw("var __ok = AINS.ID.applyTemplateStyles(__template);")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "apply_template_styles").data)

    # --------------------------------------------------------------- build
    def build_document(
        self,
        plan: LayoutPlan,
        template: TemplateSpec,
        *,
        template_document: str | None = None,
    ) -> dict[str, Any]:
        """Create the document and build every page of *plan* in one call."""
        self.connect()
        script = build_document_script(
            plan.pages,
            template,
            page_count=len(plan.pages),
            template_document=template_document or template.indesign_template_path,
        )
        self._emit(EventType.ADOBE_COMMAND, host="indesign", command="build_document", pages=len(plan.pages))
        result = self._run(script, "build_document")
        data = dict(result.data or {})
        data["strategy"] = result.strategy
        overflow = data.get("overflow") or []
        if overflow:
            log.warning("%d frame(s) overflow after building the document", len(overflow))
        return data

    def build_page(self, page: PageLayout, template: TemplateSpec) -> dict[str, Any]:
        """Build a single page in the open document."""
        self.connect()
        script = build_page_script(page, template)
        self._emit(EventType.ADOBE_COMMAND, host="indesign", command="build_page", page=page.index)
        return dict(self._run(script, f"build_page_{page.index}").data or {})

    def create_text_frame(
        self, page_index: int, element: ElementSpec, template: TemplateSpec
    ) -> dict[str, Any]:
        """Create one text frame."""
        payload = element_payload(element, template)
        builder = ScriptBuilder("indesign", "create_text_frame")
        if element.typography is not None:
            builder.call("ensureParagraphStyle", typography_payload(element.typography, element.style_id))
            payload["style_name"] = element.typography.style_name
        builder.call("createTextFrame", page_index, payload, assign="__frame")
        builder.emit("__frame")
        return dict(self._run(builder.build(), "create_text_frame").data or {})

    def create_image_frame(
        self, page_index: int, element: ElementSpec, template: TemplateSpec
    ) -> dict[str, Any]:
        """Create one image frame and place its picture."""
        builder = ScriptBuilder("indesign", "create_image_frame")
        builder.call("createImageFrame", page_index, element_payload(element, template), assign="__frame")
        builder.emit("__frame")
        return dict(self._run(builder.build(), "create_image_frame").data or {})

    def place_image(self, frame_name: str, path: Path | str, fit_mode: str = "fill") -> bool:
        """Place an image into an existing frame."""
        builder = ScriptBuilder("indesign", "place_image")
        builder.call("placeImage", frame_name, str(path), fit_mode, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "place_image").data)

    def insert_text(self, frame_name: str, text: str, style_name: str | None = None) -> dict[str, Any]:
        """Replace the contents of a text frame."""
        builder = ScriptBuilder("indesign", "insert_text")
        builder.call("setText", frame_name, text, style_name or "", assign="__r")
        builder.emit("__r")
        return dict(self._run(builder.build(), "insert_text").data or {})

    def apply_paragraph_style(self, frame_name: str, style_name: str) -> bool:
        """Apply a paragraph style to a frame's whole story."""
        builder = ScriptBuilder("indesign", "apply_paragraph_style")
        builder.call("applyParagraphStyle", frame_name, style_name, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "apply_paragraph_style").data)

    def apply_character_style(self, frame_name: str, style_name: str, start: int, length: int) -> bool:
        """Apply a character style to a range of characters."""
        builder = ScriptBuilder("indesign", "apply_character_style")
        builder.call("applyCharacterStyle", frame_name, style_name, start, length, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "apply_character_style").data)

    def thread_frames(self, from_frame: str, to_frame: str) -> dict[str, Any]:
        """Link two frames so a story continues."""
        builder = ScriptBuilder("indesign", "thread_frames")
        builder.call("threadFrames", from_frame, to_frame, assign="__r")
        builder.emit("__r")
        return dict(self._run(builder.build(), "thread_frames").data or {})

    # ------------------------------------------------------------ geometry
    def move_element(self, frame_name: str, dx_mm: float, dy_mm: float) -> dict[str, Any]:
        """Move a frame by a millimetre delta."""
        builder = ScriptBuilder("indesign", "move_element")
        builder.call("moveElement", frame_name, dx_mm, dy_mm, assign="__g")
        builder.emit("__g")
        return dict(self._run(builder.build(), "move_element").data or {})

    def set_bounds(self, frame_name: str, rect: Rect) -> dict[str, Any]:
        """Set a frame's exact geometry."""
        builder = ScriptBuilder("indesign", "set_bounds")
        builder.call(
            "setBounds",
            frame_name,
            {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height},
            assign="__g",
        )
        builder.emit("__g")
        return dict(self._run(builder.build(), "set_bounds").data or {})

    def resize_element(self, frame_name: str, factor: float) -> dict[str, Any]:
        """Scale a frame about its top-left corner."""
        builder = ScriptBuilder("indesign", "resize_element")
        builder.call("resizeElement", frame_name, factor, assign="__g")
        builder.emit("__g")
        return dict(self._run(builder.build(), "resize_element").data or {})

    def align_element(self, frame_name: str, mode: str) -> dict[str, Any]:
        """Align a frame to its page."""
        builder = ScriptBuilder("indesign", "align_element")
        builder.call("alignToPage", frame_name, mode, assign="__g")
        builder.emit("__g")
        return dict(self._run(builder.build(), "align_element").data or {})

    def delete_element(self, frame_name: str) -> bool:
        """Delete a frame."""
        builder = ScriptBuilder("indesign", "delete_element")
        builder.call("deleteElement", frame_name, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "delete_element").data)

    def fit_image(self, frame_name: str, fit_mode: str = "fill") -> bool:
        """Re-fit the picture inside a frame."""
        builder = ScriptBuilder("indesign", "fit_image")
        builder.call("fitImage", frame_name, fit_mode, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "fit_image").data)

    # ---------------------------------------------------------- inspection
    def detect_overflow(self) -> list[dict[str, Any]]:
        """Frames whose text does not fit, as reported by InDesign itself."""
        builder = ScriptBuilder("indesign", "detect_overflow")
        builder.call("detectOverflow", assign="__o")
        builder.emit("__o")
        return list(self._run(builder.build(), "detect_overflow").data or [])

    def page_report(self, page_index: int) -> dict[str, Any]:
        """Measured geometry of every item on a page (the QA ground truth)."""
        builder = ScriptBuilder("indesign", "page_report")
        builder.call("pageReport", page_index, assign="__r")
        builder.emit("__r")
        return dict(self._run(builder.build(), "page_report").data or {})

    def link_report(self) -> list[dict[str, Any]]:
        """Status of every placed link."""
        builder = ScriptBuilder("indesign", "link_report")
        builder.call("linkReport", assign="__r")
        builder.emit("__r")
        return list(self._run(builder.build(), "link_report").data or [])

    def update_links(self) -> int:
        """Update out-of-date links; returns how many were refreshed."""
        builder = ScriptBuilder("indesign", "update_links")
        builder.call("updateLinks", assign="__n")
        builder.emit("__n")
        return int(self._run(builder.build(), "update_links").data or 0)

    # -------------------------------------------------------------- output
    def render_preview(self, page_index: int, target: Path | str, dpi: int = 110, fmt: str = "png") -> Path:
        """Export one page as an image for the Vision QA stage."""
        target = Path(target)
        builder = ScriptBuilder("indesign", "render_preview")
        builder.call("exportPagePreview", str(target), page_index, dpi, fmt, assign="__r")
        builder.emit("__r")
        result = self._run(builder.build(), "render_preview")
        data = result.data or {}
        if not data.get("exists"):
            raise ScriptExecutionError(
                f"InDesign did not produce the preview for page {page_index}",
                context={"target": str(target)},
            )
        self._emit(EventType.PREVIEW_READY, page=page_index, path=str(target))
        return target

    def export_pdf(self, target: Path | str, preset: PDFPresetSpec | None = None) -> Path:
        """Export the document to PDF using a preset or explicit settings."""
        target = Path(target)
        options = {
            "marks": preset.include_marks if preset else True,
            "bleed": preset.include_bleed if preset else True,
            "dpi": preset.downsample_dpi if preset else 300,
        }
        builder = ScriptBuilder("indesign", "export_pdf")
        builder.call(
            "exportPDF", str(target), preset.indesign_preset if preset else "", options, assign="__r"
        )
        builder.emit("__r")
        result = self._run(builder.build(), "export_pdf")
        if not (result.data or {}).get("exists"):
            raise ScriptExecutionError(
                "InDesign reported the PDF export did not produce a file",
                context={"target": str(target)},
            )
        self._emit(EventType.EXPORT_READY, kind="pdf", path=str(target))
        return target

    def export_idml(self, target: Path | str) -> Path:
        """Export the document as IDML."""
        target = Path(target)
        builder = ScriptBuilder("indesign", "export_idml")
        builder.call("exportIDML", str(target), assign="__r")
        builder.emit("__r")
        self._run(builder.build(), "export_idml")
        return target

    def save(self, target: Path | str | None = None) -> Path | None:
        """Save the document (as ``.indd``)."""
        builder = ScriptBuilder("indesign", "save")
        builder.call("save", str(target) if target else "", assign="__r")
        builder.emit("__r")
        result = self._run(builder.build(), "save")
        path = (result.data or {}).get("path")
        self.document_path = Path(path) if path else (Path(target) if target else None)
        return self.document_path

    def package(self, folder: Path | str) -> Path:
        """Package the document with its links and fonts."""
        builder = ScriptBuilder("indesign", "package")
        builder.call("packageDocument", str(folder), assign="__r")
        builder.emit("__r")
        self._run(builder.build(), "package")
        return Path(folder)

    def close_document(self, save: bool = False) -> bool:
        """Close the active document."""
        builder = ScriptBuilder("indesign", "close_document")
        builder.call("closeDocument", save, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "close_document").data)

    def close_all(self) -> bool:
        """Close every open document without saving."""
        builder = ScriptBuilder("indesign", "close_all")
        builder.call("closeAll", assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "close_all").data)

    # ------------------------------------------------------------ internals
    def _run(self, script: Any, operation: str) -> ScriptResult:
        """Execute a script and raise on a reported error."""
        if not self._connected:
            self.connect()
        result = self.bridge.run(script, operation=operation)
        return result.raise_for_status(operation)

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return self.bridge.describe()
