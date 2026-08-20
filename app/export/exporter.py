"""Export.

Produces the deliverables of specification §41/§42: PDFs in the selected
presets, the InDesign document and its IDML companion, page previews and a
project archive. InDesign is the source of truth: a PDF is only produced by
the built-in renderer when InDesign is unavailable, and the result says so.
"""

from __future__ import annotations

import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adobe.indesign.controller import InDesignController
from app.core.errors import ExportError
from app.core.events import EventBus, EventType
from app.models.schemas import LayoutPlan
from app.templates.schema import PDFPresetSpec, TemplateSpec
from app.utils import imaging
from app.utils.files import make_archive
from app.vision.renderer import PreviewRenderer


def pdf_from_images(images: list[Path], target: Path, source_dpi: int, target_dpi: int) -> Path:
    """Write a multi-page PDF from already-rendered page images.

    A thin alias for :func:`app.utils.imaging.write_pdf`; the built-in
    renderer uses the same helper for its fallback output.
    """
    return imaging.write_pdf(images, target, source_dpi, target_dpi)


def scale_image(source: Path, target: Path, source_dpi: int, target_dpi: int) -> Path:
    """Downsample a rendered page to another resolution."""
    from PIL import Image

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ratio = min(1.0, target_dpi / max(1, source_dpi))
    with Image.open(source) as image:
        if ratio < 0.999:
            image = image.resize(
                (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        image.convert("RGB").save(target, dpi=(target_dpi, target_dpi))
    return target


def write_jpeg(source: Path, target: Path, quality: int = 88) -> Path:
    """Write a JPEG companion for a rendered page."""
    from PIL import Image

    target = Path(target)
    with Image.open(source) as image:
        image.convert("RGB").save(target, "JPEG", quality=quality, optimize=True)
    return target


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
    assets: list[str] = field(default_factory=list)
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
            "assets": self.assets,
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
        *,
        builtin_pdf_dpi: int = 200,
        render_workers: int = 4,
    ) -> None:
        self.indesign = indesign
        self.bus = bus
        self.builtin_pdf_dpi = max(72, builtin_pdf_dpi)
        self.render_workers = max(1, render_workers)
        # One service instance is shared by the pipeline and the Export page,
        # and an export mutates the InDesign document; two at once would
        # interleave their scripts.
        self._lock = threading.RLock()

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
        with self._lock:
            return self._export(
                handle,
                plan,
                template,
                presets=presets,
                export_indd=export_indd,
                export_idml=export_idml,
                export_previews=export_previews,
                preview_dpi=preview_dpi,
                archive=archive,
            )

    def _export(
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
        """Body of :meth:`export`, run under the service lock."""
        result = ExportResult()
        masters: tuple[int, list[Path]] | None = None
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
            masters = self._builtin_pdfs(handle, plan, template, wanted, available, preview_dpi, result)
            result.warnings.append(
                "The PDF was rendered by the built-in engine, not InDesign; "
                "colour management and preflight settings are not applied."
            )

        if export_previews:
            result.previews = self._previews(
                handle, plan, template, preview_dpi, use_indesign, result, masters
            )

        if result.pdfs:
            primary = Path(result.primary_pdf or "")
            final = output / "newspaper_final.pdf"
            if primary.exists() and primary != final:
                shutil.copy2(primary, final)
                result.pdfs["final"] = str(final)

        result.assets = self._collect_assets(handle, plan)

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

    def _builtin_pdfs(
        self,
        handle: ProjectHandle,
        plan: LayoutPlan,
        template: TemplateSpec,
        wanted: list[str],
        available: dict[str, PDFPresetSpec],
        preview_dpi: int,
        result: ExportResult,
    ) -> tuple[int, list[Path]]:
        """Produce every requested PDF from a single set of page renders.

        Rendering each page once at the highest resolution any output needs,
        then downsampling, turns an N-preset export from N full renders into
        one - which on a twenty-page broadsheet is the difference between a
        minute and a few seconds.
        """
        target_dpis = {
            preset_id: min(300, available[preset_id].downsample_dpi) if preset_id in available else 200
            for preset_id in wanted
        }
        # The built-in PDF is a proof; its resolution is capped so a large
        # edition does not spend minutes rasterising at press resolution.
        master_dpi = min(max([*target_dpis.values(), preview_dpi, 150]), self.builtin_pdf_dpi)
        renderer = PreviewRenderer(template, dpi=master_dpi)
        masters = self._render_pages(handle, plan, renderer, master_dpi)
        if not masters:
            raise ExportError("The built-in renderer produced no pages to export")

        output = handle.output_dir
        for preset_id in wanted:
            target = output / PRESET_FILENAMES.get(preset_id, f"newspaper_{preset_id}.pdf")
            try:
                pdf_from_images(masters, target, master_dpi, target_dpis[preset_id])
                result.pdfs[preset_id] = str(target)
            except Exception as exc:  # noqa: BLE001
                raise ExportError(f"Could not produce the '{preset_id}' PDF: {exc}", cause=exc) from exc
        return (master_dpi, masters)

    def _render_pages(
        self,
        handle: ProjectHandle,
        plan: LayoutPlan,
        renderer: PreviewRenderer,
        dpi: int,
    ) -> list[Path]:
        """Render every page once, caching the result for this export."""
        directory = handle.directory / "previews" / f"master_{dpi}"
        directory.mkdir(parents=True, exist_ok=True)
        targets = [(page, directory / f"page_{page.index:03d}.png") for page in plan.pages]

        def render(item: tuple[Any, Path]) -> Path:
            page, target = item
            renderer.render_page(page, target)
            return target

        if len(targets) > 1 and self.render_workers > 1:
            # Page rasterisation is independent work and FreeType releases the
            # interpreter lock, so it genuinely parallelises.
            with ThreadPoolExecutor(
                max_workers=min(self.render_workers, len(targets)),
                thread_name_prefix="ains-render",
            ) as pool:
                paths = list(pool.map(render, targets))
        else:
            paths = [render(item) for item in targets]
        return paths

    def _collect_assets(self, handle: ProjectHandle, plan: LayoutPlan) -> list[str]:
        """Copy the pictures the pages actually use into ``output/assets``."""
        directory = handle.output_dir / "assets"
        directory.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        seen: set[str] = set()
        for page in plan.pages:
            for element in page.elements:
                path = element.image_path
                if not path or path in seen:
                    continue
                seen.add(path)
                source = Path(path)
                if not source.exists():
                    continue
                destination = directory / source.name
                try:
                    if not destination.exists() or destination.stat().st_size != source.stat().st_size:
                        shutil.copy2(source, destination)
                    copied.append(str(destination))
                except OSError as exc:  # pragma: no cover - disk problems
                    log.warning("Could not copy %s into the output folder: %s", source.name, exc)
        return copied

    def _previews(
        self,
        handle: ProjectHandle,
        plan: LayoutPlan,
        template: TemplateSpec,
        dpi: int,
        use_indesign: bool,
        result: ExportResult,
        masters: tuple[int, list[Path]] | None = None,
    ) -> list[str]:
        """Export a page image per page into ``output/previews``.

        Both a PNG and a JPEG are written for every page (specification §41).
        When the built-in renderer already produced master images for the PDFs
        they are downsampled rather than rendered again.
        """
        directory = handle.output_dir / "previews"
        directory.mkdir(parents=True, exist_ok=True)
        previews: list[str] = []
        renderer = PreviewRenderer(template, dpi=dpi)

        for index, page in enumerate(plan.pages):
            png = directory / f"page_{page.index:03d}.png"
            jpeg = directory / f"page_{page.index:03d}.jpg"
            if use_indesign and self.indesign is not None:
                try:
                    self.indesign.render_preview(page.index, jpeg, dpi=dpi, fmt="jpeg")
                    previews.append(str(jpeg))
                    continue
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"InDesign preview for page {page.index} failed: {exc}")
            try:
                if masters and index < len(masters[1]):
                    scale_image(masters[1][index], png, masters[0], dpi)
                else:
                    renderer.render_page(page, png)
                write_jpeg(png, jpeg)
                previews.extend([str(png), str(jpeg)])
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
