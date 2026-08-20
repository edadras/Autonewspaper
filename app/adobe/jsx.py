"""ExtendScript generation.

Python never drives the Adobe UI frame by frame: it generates one JSX program
per operation (or per page), sends it to the host application and reads back a
structured JSON result. This module assembles those programs from the runtime
library in ``app/adobe/scripts`` plus a generated body.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models.schemas import ElementSpec, PageLayout, TypographySpec
from app.templates.schema import TemplateSpec

log = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

HOST_LIBRARIES = {
    "indesign": ["json2.jsx", "ains_core.jsx", "indesign_lib.jsx"],
    "photoshop": ["json2.jsx", "ains_core.jsx", "photoshop_lib.jsx"],
}


def js(value: Any) -> str:
    """Serialise a Python value into an ExtendScript literal.

    JSON is a subset of ECMAScript, so ``json.dumps`` produces valid source;
    ``ensure_ascii`` keeps non-ASCII text as ``\\uXXXX`` escapes, which is the
    only encoding-safe way to embed Persian text in a JSX file.
    """
    return json.dumps(value, ensure_ascii=True, default=str)


@lru_cache(maxsize=8)
def library_text(host: str) -> str:
    """Concatenated runtime library for *host* (cached)."""
    names = HOST_LIBRARIES.get(host)
    if not names:
        raise ValueError(f"Unknown Adobe host: {host}")
    parts: list[str] = []
    for name in names:
        path = SCRIPTS_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"Missing JSX runtime file: {path}")
        parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


@dataclass
class Script:
    """A generated ExtendScript program."""

    host: str
    body: str
    result_path: Path | None = None
    name: str = "operation"
    metadata: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        """Full program text, ready to run."""
        result_line = (
            f"AINS.setResultPath({js(str(self.result_path))});" if self.result_path else ""
        )
        return "\n".join(
            [
                "// AI Newspaper Studio - generated script",
                f"// operation: {self.name}",
                library_text(self.host),
                "",
                "(function () {",
                "    var __out;",
                "    try {",
                f"        {result_line}",
                _indent(self.body, 8),
                "    } catch (e) {",
                "        __out = AINS.fail(e, " + js(self.name) + ");",
                "        return __out;",
                "    }",
                "    return __out;",
                "}());",
                "",
            ]
        )

    def save(self, path: Path) -> Path:
        """Write the program to *path* as UTF-8."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render(), encoding="utf-8")
        return target


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


class ScriptBuilder:
    """Fluent builder for a JSX body."""

    def __init__(self, host: str, name: str = "operation") -> None:
        self.host = host
        self.name = name
        self.namespace = "AINS.ID" if host == "indesign" else "AINS.PS"
        self._lines: list[str] = []

    def raw(self, code: str) -> ScriptBuilder:
        """Append raw ExtendScript."""
        self._lines.append(code)
        return self

    def call(self, method: str, *args: Any, assign: str | None = None) -> ScriptBuilder:
        """Append a call to a runtime library method."""
        rendered = ", ".join(js(arg) for arg in args)
        statement = f"{self.namespace}.{method}({rendered});"
        if assign:
            statement = f"var {assign} = {self.namespace}.{method}({rendered});"
        self._lines.append(statement)
        return self

    def var(self, name: str, value: Any) -> ScriptBuilder:
        """Declare a variable holding a serialised Python value."""
        self._lines.append(f"var {name} = {js(value)};")
        return self

    def log(self, message: str) -> ScriptBuilder:
        """Append a log line."""
        self._lines.append(f"AINS.log({js(message)});")
        return self

    def emit(self, expression: str) -> ScriptBuilder:
        """Emit *expression* as the successful result of the script."""
        self._lines.append(f"__out = AINS.emit(true, {expression});")
        return self

    def build(self, result_path: Path | None = None, **metadata: Any) -> Script:
        """Produce the :class:`Script`."""
        return Script(
            host=self.host,
            body="\n".join(self._lines),
            result_path=result_path,
            name=self.name,
            metadata=metadata,
        )


# ------------------------------------------------------------- converters --


def typography_payload(typography: TypographySpec | None, style_id: str) -> dict[str, Any]:
    """Serialise a resolved typography spec for ``ensureParagraphStyle``."""
    if typography is None:
        return {"style_name": style_id or "Body"}
    return {
        "style_name": typography.style_name or style_id or "Body",
        "font_family": typography.font_family,
        "font_style": typography.font_style,
        "fallback_fonts": typography.fallback_fonts,
        "size_pt": typography.size_pt,
        "leading_pt": typography.leading_pt,
        "tracking": typography.tracking,
        "alignment": typography.alignment,
        "direction": typography.direction,
        "color": typography.color,
        "space_before_pt": typography.space_before_pt,
        "space_after_pt": typography.space_after_pt,
        "hyphenation": typography.hyphenation,
        "all_caps": typography.all_caps,
    }


def element_payload(element: ElementSpec, template: TemplateSpec) -> dict[str, Any]:
    """Serialise a layout element into the shape ``buildPage`` expects."""
    object_style = "image_frame" if element.is_image else "text_frame"
    if element.style_id and template.object_style(element.style_id):
        object_style = element.style_id
    payload: dict[str, Any] = {
        "id": element.frame_name or element.id,
        "kind": "image" if element.is_image else "text",
        "rect": {
            "x": round(element.rect.x, 3),
            "y": round(element.rect.y, 3),
            "width": round(element.rect.width, 3),
            "height": round(element.rect.height, 3),
        },
        "rotation": element.rotation,
        "object_style": object_style,
        "text_wrap_mm": element.text_wrap_mm or (3.0 if element.is_image else 0.0),
        "stroke_weight_pt": element.stroke_weight_pt,
    }
    if element.is_image:
        payload.update(
            {
                "image_path": element.image_path or "",
                "fit_mode": element.fit_mode,
            }
        )
    else:
        typography = element.typography
        payload.update(
            {
                "text": element.text,
                "style_name": (typography.style_name if typography else element.style_id) or "Body",
                "columns": max(1, typography.columns if typography else element.column_span),
                "column_gutter_mm": typography.column_gutter_mm if typography else template.grid.gutter_mm,
                "direction": typography.direction if typography else template.direction,
                "inset_mm": 1.0,
            }
        )
    return payload


def page_payload(page: PageLayout, template: TemplateSpec) -> dict[str, Any]:
    """Serialise a whole page plan."""
    return {
        "index": page.index,
        "margin_top_mm": page.margin_top_mm,
        "margin_bottom_mm": page.margin_bottom_mm,
        "margin_inside_mm": page.margin_inside_mm,
        "margin_outside_mm": page.margin_outside_mm,
        "columns": page.columns,
        "gutter_mm": page.gutter_mm,
        "master": page.master,
        "section": page.section,
        "elements": [
            element_payload(element, template)
            for element in sorted(page.elements, key=lambda e: e.z_index)
        ],
    }


def template_payload(template: TemplateSpec, typographies: list[dict[str, Any]]) -> dict[str, Any]:
    """Serialise the styles and colours a document needs."""
    return {
        "id": template.id,
        "colors": [color.model_dump() for color in template.colors],
        "paragraph_styles": typographies,
        "object_styles": [style.model_dump() for style in template.object_styles],
        "direction": template.direction,
    }


def collect_paragraph_styles(page: PageLayout, template: TemplateSpec) -> list[dict[str, Any]]:
    """Unique paragraph styles used by a page, resolved to concrete values.

    Every frame carries its own auto-fitted size, so a style is emitted per
    distinct ``(style name, size, leading)`` combination and the frame name
    encodes which one it uses.
    """
    seen: dict[str, dict[str, Any]] = {}
    for element in page.elements:
        if not element.is_text or element.typography is None:
            continue
        typography = element.typography
        name = f"{typography.style_name} {typography.size_pt:g}/{typography.leading_pt:g}"
        payload = typography_payload(typography, element.style_id)
        payload["style_name"] = name
        seen[name] = payload
        element.meta["indesign_style"] = name
    return list(seen.values())


def build_page_script(
    page: PageLayout, template: TemplateSpec, result_path: Path | None = None
) -> Script:
    """Generate the script that builds one page in an open document."""
    styles = collect_paragraph_styles(page, template)
    payload = page_payload(page, template)
    for element, spec in zip(
        sorted(page.elements, key=lambda e: e.z_index), payload["elements"], strict=False
    ):
        if not element.is_image and element.meta.get("indesign_style"):
            spec["style_name"] = element.meta["indesign_style"]

    builder = ScriptBuilder("indesign", name=f"build_page_{page.index}")
    builder.call("setup", {"enableRedraw": False})
    builder.var("__template", template_payload(template, styles))
    builder.var("__page", payload)
    builder.raw("AINS.ID.applyTemplateStyles(__template);")
    builder.raw("var __result = AINS.ID.buildPage(__page, __template);")
    builder.raw("__result.overflow = AINS.ID.detectOverflow();")
    builder.raw("AINS.ID.teardown();")
    builder.emit("__result")
    return builder.build(result_path, page=page.index)


def build_document_script(
    pages: list[PageLayout],
    template: TemplateSpec,
    *,
    page_count: int,
    result_path: Path | None = None,
    template_document: str | None = None,
) -> Script:
    """Generate the script that creates the document and builds every page."""
    all_styles: dict[str, dict[str, Any]] = {}
    page_payloads: list[dict[str, Any]] = []
    for page in pages:
        for style in collect_paragraph_styles(page, template):
            all_styles[style["style_name"]] = style
        payload = page_payload(page, template)
        for element, spec in zip(
            sorted(page.elements, key=lambda e: e.z_index), payload["elements"], strict=False
        ):
            if not element.is_image and element.meta.get("indesign_style"):
                spec["style_name"] = element.meta["indesign_style"]
        page_payloads.append(payload)

    builder = ScriptBuilder("indesign", name="build_document")
    builder.call("setup", {"enableRedraw": False})
    if template_document:
        builder.call("openTemplate", str(template_document), True)
        builder.call("ensurePages", page_count)
    else:
        builder.call(
            "createDocument",
            {
                "page_width_mm": template.page_width_mm,
                "page_height_mm": template.page_height_mm,
                "facing_pages": template.facing_pages,
                "page_count": page_count,
                "bleed_mm": template.bleed_mm,
            },
        )
    builder.var("__template", template_payload(template, list(all_styles.values())))
    builder.var("__plan", {"pages": page_payloads})
    builder.raw("AINS.ID.applyTemplateStyles(__template);")
    builder.raw("var __result = AINS.ID.buildDocument(__plan, __template);")
    builder.raw("__result.info = AINS.ID.info();")
    builder.raw("AINS.ID.teardown();")
    builder.emit("__result")
    return builder.build(result_path, pages=len(pages))


def build_photoshop_script(
    spec: dict[str, Any], result_path: Path | None = None, name: str = "process_image"
) -> Script:
    """Generate a Photoshop image-processing script."""
    builder = ScriptBuilder("photoshop", name=name)
    builder.call("setup")
    builder.var("__spec", spec)
    builder.raw("var __result = AINS.PS.processImage(__spec);")
    builder.raw("__result.capabilities = AINS.PS.capabilities();")
    builder.emit("__result")
    return builder.build(result_path)


def build_probe_script(host: str, result_path: Path | None = None) -> Script:
    """Generate the health-check script used by System Diagnostics."""
    builder = ScriptBuilder(host, name="probe")
    if host == "indesign":
        builder.raw("var __info = AINS.ID.setup({});")
        builder.raw("__info.documents = app.documents.length;")
        builder.raw("__info.pdf_presets = [];")
        builder.raw("for (var i = 0; i < app.pdfExportPresets.length; i++) {")
        builder.raw("    __info.pdf_presets.push(String(app.pdfExportPresets[i].name));")
        builder.raw("}")
        builder.raw("__info.fonts = app.fonts.length;")
        builder.raw("AINS.ID.teardown();")
    else:
        builder.raw("var __info = AINS.PS.setup();")
        builder.raw("__info.capabilities = AINS.PS.capabilities();")
        builder.raw("__info.documents = app.documents.length;")
    builder.emit("__info")
    return builder.build(result_path)
