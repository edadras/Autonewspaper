"""Export.

Produces the deliverables of specification §41/§42: PDFs in the selected
presets, the InDesign document and its IDML companion, page previews and a
project archive. InDesign is the source of truth: a PDF is only produced by
the built-in renderer when InDesign is unavailable, and the result says so.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adobe.indesign.controller import InDesignController
from app.core.errors import ExportError
from app.core.events import EventBus, EventType
from app.models.schemas import LayoutPlan
from app.templates.schema import PDFPresetSpec, TemplateSpec
from app.utils.files import make_archive
from app.vision.renderer import PreviewRenderer

if TYPE_CHECKING:  # pragma: no cover - avoids an import cycle with app.services
    from app.services.project_manager import ProjectHandle

log = logging.getLogger(__name__)

PRESET_FILENAMES = {
    "print": "newspaper_print.pdf",
    "high_quality": "newspaper_highquality.pdf",
    "digital": "newspaper_digital.pdf",
    "web": "newspaper_web.pdf",
}

FALLBACK_PRESETS = [
    PDFPresetSpec(id="print", label="Print", indesign_preset="[Press Quality]", downsample_dpi=300),
    PDFPresetSpec(
        id="high_quality",
        label="High quality",
        indesign_preset="[High Quality Print]",
        downsample_dpi=350,
    ),
    PDFPresetSpec(
        id="digital",
        label="Digital",
        indesign_preset="[High Quality Print]",
        color_space="RGB",
        include_bleed=False,
        include_marks=False,
        downsample_dpi=180,
    ),
    PDFPresetSpec(
        id="web",
        label="Web",
        indesign_preset="[Smallest File Size]",
        color_space="RGB",
        include_bleed=False,
        include_marks=False,
        downsample_dpi=110,
    ),
]


@dataclass
class ExportResult:
    """Everything an export produced."""

    pdfs: dict[str, str] = field(default_factory=dict)
    indd: str | None = None
    idml: str | None = None
    previews: list[str] = field(default_factory=list)
    archive: str | None = None
    engine: str = "indesign"
    """``"indesign"`` or ``"builtin"`` - which renderer produced the PDFs."""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "pdfs": self.pdfs,
            "indd": self.indd,
            "idml": self.idml,
            "previews": self.previews,
            "archive": self.archive,
            "engine": self.engine,
            "warnings": self.warnings,
        }

    @property
    def primary_pdf(self) -> str | None:
        """The PDF to open when the operator clicks "Open result"."""
        for preset in ("print", "high_quality", "digital", "web"):
            if preset in self.pdfs:
                return self.pdfs[preset]
        return next(iter(self.pdfs.values()), None)


class ExportService:
    """Writes the final deliverables of a project."""

    def __init__(
        self,
        indesign: InDesignController | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.indesign = indesign
        self.bus = bus

    # ---------------------------------------------------------------- pdfs
    def export(
        self,
        handle: ProjectHandle,
        plan: LayoutPlan,
        template: TemplateSpec,
        *,
        presets: list[str] | None = None,
        export_indd: bool = True,
        export_idml: bool = True,
        export_previews: bool = True,
        preview_dpi: int = 110,
        archive: bool = False,
    ) -> ExportResult:
        """Produce every requested deliverable."""
        result = ExportResult()
        output = handle.output_dir
        output.mkdir(parents=True, exist_ok=True)
        (output / "previews").mkdir(parents=True, exist_ok=True)

        wanted = presets or ["print"]
        available = {preset.id: preset for preset in (template.pdf_presets or FALLBACK_PRESETS)}

        use_indesign = self.indesign is not None and self.indesign.available()
        if use_indesign:
            result.engine = "indesign"
            for preset_id in wanted:
                preset = available.get(preset_id) or available.get("print")
                target = output / PRESET_FILENAMES.get(preset_id, f"newspaper_{preset_id}.pdf")
                try:
                    assert self.indesign is not None
                    self.indesign.export_pdf(target, preset)
                    result.pdfs[preset_id] = str(target)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"InDesign PDF export '{preset_id}' failed: {exc}")
                    log.error("PDF export '%s' failed: %s", preset_id, exc)
            if export_indd:
                try:
                    assert self.indesign is not None
                    saved = self.indesign.save(handle.adobe_dir / "indesign" / f"{handle.slug}.indd")
                    result.indd = str(saved) if saved else None
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"Saving the InDesign document failed: {exc}")
            if export_idml:
                try:
                    assert self.indesign is not None
                    idml = self.indesign.export_idml(handle.adobe_dir / "indesign" / f"{handle.slug}.idml")
                    result.idml = str(idml)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"IDML export failed: {exc}")

        if not result.pdfs:
            # Either InDesign is absent or every export attempt failed.
            result.engine = "builtin"
            if use_indesign:
                result.warnings.append("InDesign produced no PDF; the built-in renderer was used instead.")
            renderer = PreviewRenderer(template, dpi=max(150, preview_dpi))
            for preset_id in wanted:
                preset = available.get(preset_id)
                dpi = min(300, preset.downsample_dpi if preset else 200)
                target = output / PRESET_FILENAMES.get(preset_id, f"newspaper_{preset_id}.pdf")
                try:
                    renderer.render_pdf(plan, target, dpi=dpi)
                    result.pdfs[preset_id] = str(target)
                except Exception as exc:  # noqa: BLE001
                    raise ExportError(f"Could not produce the '{preset_id}' PDF: {exc}", cause=exc) from exc
            result.warnings.append(
                "The PDF was rendered by the built-in engine, not InDesign; "
                "colour management and preflight settings are not applied."
            )

        if export_previews:
            result.previews = self._previews(handle, plan, template, preview_dpi, use_indesign, result)

        if result.pdfs:
            primary = Path(result.primary_pdf or "")
            final = output / "newspaper_final.pdf"
            if primary.exists() and primary != final:
                shutil.copy2(primary, final)
                result.pdfs["final"] = str(final)

        if archive:
            try:
                result.archive = str(
                    make_archive(
                        handle.directory,
                        output / "archive" / f"{handle.slug}.zip",
                        exclude={"versions"},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"Archiving failed: {exc}")

        self._emit(EventType.EXPORT_READY, slug=handle.slug, **result.to_dict())
        log.info(
            "Exported %d PDF(s) with the %s engine into %s",
            len(result.pdfs),
            result.engine,
            output,
        )
        return result

    def _previews(
        self,
        handle: ProjectHandle,
        plan: LayoutPlan,
        template: TemplateSpec,
        dpi: int,
        use_indesign: bool,
        result: ExportResult,
    ) -> list[str]:
        """Export a page image per page into ``output/previews``."""
        directory = handle.output_dir / "previews"
        previews: list[str] = []
        renderer = PreviewRenderer(template, dpi=dpi)
        for page in plan.pages:
            target = directory / f"page_{page.index:03d}.jpg"
            if use_indesign and self.indesign is not None:
                try:
                    self.indesign.render_preview(page.index, target, dpi=dpi, fmt="jpeg")
                    previews.append(str(target))
                    continue
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"InDesign preview for page {page.index} failed: {exc}")
            try:
                png = target.with_suffix(".png")
                renderer.render_page(page, png)
                previews.append(str(png))
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"Preview for page {page.index} failed: {exc}")
        return previews

    # ------------------------------------------------------------- helpers
    def available_presets(self, template: TemplateSpec) -> list[dict[str, Any]]:
        """Presets the operator can choose on the Export page."""
        presets = template.pdf_presets or FALLBACK_PRESETS
        return [
            {
                "id": preset.id,
                "label": preset.label or preset.id.replace("_", " ").title(),
                "color_space": preset.color_space,
                "bleed": preset.include_bleed,
                "marks": preset.include_marks,
                "dpi": preset.downsample_dpi,
                "indesign_preset": preset.indesign_preset,
            }
            for preset in presets
        ]

    def package(self, handle: ProjectHandle, folder: Path | None = None) -> Path | None:
        """Package the InDesign document with its links and fonts."""
        if self.indesign is None or not self.indesign.available():
            log.info("Packaging requires InDesign; skipped")
            return None
        target = folder or handle.output_dir / "archive" / "package"
        return self.indesign.package(target)

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)
