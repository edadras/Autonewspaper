"""Asset management.

Imports pictures, measures them (specification §39), removes duplicates,
generates the missing ones with the configured image provider (§5, §40),
processes them for print through Photoshop or the local engine, and picks the
best picture for each story.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adobe.photoshop.controller import PhotoshopController
from app.ai.registry import AIService
from app.core.errors import AssetError
from app.core.events import EventBus, EventType
from app.core.jobs import JobContext, JobQueue
from app.models import entities as E  # noqa: N812
from app.models.schemas import ImageRequest
from app.services.project_manager import ProjectHandle
from app.utils import imaging
from app.utils.files import checksum, copy_into, safe_filename
from app.utils.units import aspect_ratio_size, closest_aspect_ratio, effective_dpi

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".psd", ".bmp"}

#: Hamming distance under which two perceptual hashes count as duplicates.
DUPLICATE_DISTANCE = 6


@dataclass
class AssetImportResult:
    """Outcome of importing pictures."""

    assets: list[int] = field(default_factory=list)
    duplicates: list[tuple[str, int]] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of new assets."""
        return len(self.assets)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "assets": self.assets,
            "duplicates": [{"file": f, "existing_asset": a} for f, a in self.duplicates],
            "rejected": [{"file": f, "reason": r} for f, r in self.rejected],
            "count": self.count,
        }


class AssetManager:
    """Owns everything about the pictures of a project."""

    def __init__(
        self,
        ai: AIService | None = None,
        photoshop: PhotoshopController | None = None,
        jobs: JobQueue | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.ai = ai
        self.photoshop = photoshop
        self.jobs = jobs
        self.bus = bus

    # ------------------------------------------------------------- import
    def import_files(
        self,
        handle: ProjectHandle,
        files: Iterable[Path | str],
        *,
        asset_type: str = "image",
        article_id: int | None = None,
        skip_duplicates: bool = True,
    ) -> AssetImportResult:
        """Copy, measure and register image files."""
        result = AssetImportResult()
        for item in files:
            file = Path(item)
            if not file.exists():
                result.rejected.append((str(file), "file not found"))
                continue
            if file.suffix.lower() not in IMAGE_SUFFIXES:
                result.rejected.append((file.name, f"unsupported image type '{file.suffix}'"))
                continue
            try:
                digest = checksum(file)
            except OSError as exc:
                result.rejected.append((file.name, f"unreadable: {exc}"))
                continue

            with handle.uow() as uow:
                existing = uow.assets.by_checksum(handle.project_id, digest)
                if existing is not None and skip_duplicates:
                    result.duplicates.append((file.name, existing.id))
                    continue

            try:
                destination = copy_into(file, handle.images_dir, rename=safe_filename(file.name))
                asset_id = self._register(
                    handle,
                    destination,
                    digest,
                    asset_type=asset_type,
                    article_id=article_id,
                    source="import",
                )
            except Exception as exc:  # noqa: BLE001
                result.rejected.append((file.name, str(exc)[:200]))
                log.warning("Cannot import %s: %s", file.name, exc)
                continue
            result.assets.append(asset_id)

        if result.assets:
            self._mark_near_duplicates(handle)
        self._emit(EventType.ASSET_IMPORTED, slug=handle.slug, **result.to_dict())
        return result

    def _register(
        self,
        handle: ProjectHandle,
        path: Path,
        digest: str,
        *,
        asset_type: str = "image",
        article_id: int | None = None,
        source: str = "import",
        ai_generated: bool = False,
        provider: str = "",
        model: str = "",
        prompt: str = "",
        caption: str = "",
    ) -> int:
        """Analyse a file and create its :class:`Asset` row."""
        analysis = imaging.analyze(path)
        if analysis.width == 0:
            raise AssetError(f"{path.name} is not a readable image")
        try:
            thumbnail = imaging.make_preview(path, handle.thumbnails_dir / f"{path.stem}.jpg")
        except Exception as exc:  # noqa: BLE001
            log.debug("Thumbnail failed for %s: %s", path.name, exc)
            thumbnail = None

        with handle.uow() as uow:
            row = E.Asset(
                project_id=handle.project_id,
                article_id=article_id,
                filename=path.name,
                type=asset_type,
                source=source,
                path=str(path),
                checksum=digest,
                width=analysis.width,
                height=analysis.height,
                aspect_ratio=analysis.aspect_ratio,
                dpi=analysis.dpi,
                ai_generated=ai_generated,
                quality_score=analysis.quality_score,
                blur_score=analysis.sharpness,
                face_count=analysis.face_count,
                orientation=analysis.orientation,
                provider=provider,
                model=model,
                prompt=prompt,
                caption=caption,
            )
            row.set_meta(
                {
                    "analysis": analysis.model_dump(mode="json"),
                    "thumbnail": str(thumbnail) if thumbnail else None,
                    "phash": self._safe_hash(path),
                }
            )
            uow.assets.add(row)
            return row.id

    @staticmethod
    def _safe_hash(path: Path) -> str:
        try:
            return imaging.perceptual_hash(path)
        except Exception:  # noqa: BLE001
            return ""

    def _mark_near_duplicates(self, handle: ProjectHandle) -> int:
        """Flag visually near-identical pictures (specification §39)."""
        marked = 0
        with handle.uow() as uow:
            assets = uow.assets.for_project(handle.project_id)
            hashes: list[tuple[int, str]] = []
            for asset in assets:
                digest = asset.meta.get("phash") or ""
                if not digest:
                    continue
                for other_id, other_hash in hashes:
                    if imaging.hamming_distance(digest, other_hash) <= DUPLICATE_DISTANCE:
                        if asset.duplicate_of != other_id:
                            asset.duplicate_of = other_id
                            marked += 1
                        break
                else:
                    hashes.append((asset.id, digest))
        if marked:
            log.info("Marked %d near-duplicate image(s)", marked)
        return marked

    # ------------------------------------------------------------ analysis
    def reanalyze(self, handle: ProjectHandle, asset_id: int) -> dict[str, Any]:
        """Re-measure one asset (after the operator replaced the file)."""
        with handle.uow() as uow:
            row = uow.assets.get(asset_id)
            if row is None:
                raise AssetError(f"Asset {asset_id} does not exist")
            path = Path(row.usable_path)
        analysis = imaging.analyze(path)
        with handle.uow() as uow:
            row = uow.assets.get(asset_id)
            assert row is not None
            row.width, row.height = analysis.width, analysis.height
            row.aspect_ratio = analysis.aspect_ratio
            row.dpi = analysis.dpi
            row.quality_score = analysis.quality_score
            row.blur_score = analysis.sharpness
            row.face_count = analysis.face_count
            row.orientation = analysis.orientation
            meta = row.meta
            meta["analysis"] = analysis.model_dump(mode="json")
            row.set_meta(meta)
            return row.to_dict()

    def analyze_all(self, handle: ProjectHandle, ctx: JobContext | None = None) -> int:
        """Re-measure every asset; used by the pipeline's analysis stage."""
        with handle.uow() as uow:
            ids = [a.id for a in uow.assets.for_project(handle.project_id)]
        for index, asset_id in enumerate(ids):
            if ctx is not None:
                ctx.progress((index + 1) / max(1, len(ids)), f"Analysing image {index + 1}/{len(ids)}")
            try:
                self.reanalyze(handle, asset_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot analyse asset %s: %s", asset_id, exc)
        self._mark_near_duplicates(handle)
        return len(ids)

    # ---------------------------------------------------------- assignment
    def assign(self, handle: ProjectHandle, asset_id: int, article_id: int | None) -> bool:
        """Link (or unlink) a picture and a story."""
        with handle.uow() as uow:
            row = uow.assets.get(asset_id)
            if row is None:
                return False
            row.article_id = article_id
            return True

    def best_for_article(
        self, handle: ProjectHandle, article_id: int, *, min_quality: float = 0.0
    ) -> E.Asset | None:
        """Pick the best usable picture already linked to a story."""
        with handle.uow() as uow:
            candidates = [
                a
                for a in uow.assets.for_article(article_id)
                if a.duplicate_of is None and a.quality_score >= min_quality
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda a: (a.quality_score, a.width * a.height))

    def auto_assign(self, handle: ProjectHandle) -> int:
        """Give unassigned pictures to the stories that need one.

        Highest-priority stories are served first, and the best remaining
        picture wins - a low-quality image is never chosen for a lead slot.
        """
        assigned = 0
        with handle.uow() as uow:
            articles = [
                a for a in uow.articles.by_priority(handle.project_id) if a.image_required or a.priority >= 65
            ]
            pool = [a for a in uow.assets.unassigned(handle.project_id) if a.duplicate_of is None]
            pool.sort(key=lambda a: -a.quality_score)
            for article in articles:
                if not pool:
                    break
                if any(asset.article_id == article.id for asset in uow.assets.for_article(article.id)):
                    continue
                asset = pool.pop(0)
                asset.article_id = article.id
                assigned += 1
        if assigned:
            log.info("Auto-assigned %d image(s) to stories", assigned)
        return assigned

    # ---------------------------------------------------------- generation
    def generate_for_article(
        self,
        handle: ProjectHandle,
        article_id: int,
        *,
        subject: str,
        category: str = "general",
        design_style: str = "classic",
        aspect_ratio: str = "16:9",
        frame_width_mm: float = 170.0,
        frame_height_mm: float = 96.0,
        language: str = "fa",
    ) -> int | None:
        """Generate a picture for a story and register it as an asset."""
        if self.ai is None:
            log.info("No AI service configured; skipping image generation")
            return None
        prompt_payload = self.ai.image_prompt(
            subject, category, design_style, aspect_ratio, frame_width_mm, frame_height_mm
        )
        width, height = aspect_ratio_size(aspect_ratio, 1536)
        request = ImageRequest(
            subject=subject,
            style=str(prompt_payload.get("style") or "Realistic Editorial Photography"),
            aspect_ratio=aspect_ratio,
            width_px=width,
            height_px=height,
            negative_prompt=str(prompt_payload.get("negative_prompt") or "no text, no watermark, no logo"),
            language=language,
            article_id=article_id,
        )
        if prompt_payload.get("prompt"):
            request.subject = str(prompt_payload["prompt"])

        target = handle.generated_dir / f"article_{article_id}_{aspect_ratio.replace(':', 'x')}.png"
        generated = self.ai.generate_image(request, target)
        path = Path(generated.path)
        if not path.exists():
            log.warning("Image generation produced no file for article %s", article_id)
            return None
        try:
            asset_id = self._register(
                handle,
                path,
                checksum(path),
                article_id=article_id,
                source="generated",
                ai_generated=generated.provider not in ("none", ""),
                provider=generated.provider,
                model=generated.model,
                prompt=generated.prompt,
            )
        except AssetError as exc:
            log.warning("Generated image unusable: %s", exc)
            return None
        with handle.uow() as uow:
            row = uow.assets.get(asset_id)
            if row is not None:
                meta = row.meta
                meta["generation"] = generated.metadata()
                row.set_meta(meta)
        self._emit(
            EventType.ASSET_GENERATED,
            slug=handle.slug,
            article_id=article_id,
            asset_id=asset_id,
            provider=generated.provider,
        )
        log.info("Generated image for article %s via %s", article_id, generated.provider)
        return asset_id

    def generate_missing(
        self,
        handle: ProjectHandle,
        *,
        design_style: str = "classic",
        language: str = "fa",
        ctx: JobContext | None = None,
    ) -> list[int]:
        """Generate pictures for every story that asked for one and has none."""
        with handle.uow() as uow:
            articles = uow.articles.by_priority(handle.project_id)
            needing = []
            for article in articles:
                if not (article.ai_image_required or article.image_required):
                    continue
                if uow.assets.for_article(article.id):
                    continue
                needing.append(
                    (
                        article.id,
                        article.display_title or article.summary[:120],
                        article.category,
                        article.meta.get("ai_image_prompt", ""),
                    )
                )
        created: list[int] = []
        for index, (article_id, subject, category, hint) in enumerate(needing):
            if ctx is not None:
                ctx.progress(
                    (index + 1) / max(1, len(needing)),
                    f"Generating image {index + 1}/{len(needing)}",
                )
            try:
                asset_id = self.generate_for_article(
                    handle,
                    article_id,
                    subject=hint or subject,
                    category=category,
                    design_style=design_style,
                    language=language,
                )
            except Exception as exc:  # noqa: BLE001 - a failed picture never aborts a run
                log.error("Image generation failed for article %s: %s", article_id, exc)
                continue
            if asset_id:
                created.append(asset_id)
        return created

    # ---------------------------------------------------------- processing
    def process_for_frame(
        self,
        handle: ProjectHandle,
        asset_id: int,
        *,
        frame_width_mm: float,
        frame_height_mm: float,
        min_dpi: float = 300.0,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Crop and resample a picture to the frame it will occupy."""
        with handle.uow() as uow:
            row = uow.assets.get(asset_id)
            if row is None:
                raise AssetError(f"Asset {asset_id} does not exist")
            source = Path(row.path)
            filename = safe_filename(
                f"{Path(row.filename).stem}_p{int(frame_width_mm)}x{int(frame_height_mm)}.jpg"
            )

        if not source.exists():
            raise AssetError(f"The file for asset {asset_id} is missing: {source}")

        aspect = frame_width_mm / max(1e-6, frame_height_mm)
        target_width = int(round(frame_width_mm / 25.4 * min_dpi))
        target_height = int(round(frame_height_mm / 25.4 * min_dpi))
        destination = handle.processed_dir / filename

        controller = self.photoshop
        if controller is None:
            controller = PhotoshopController(handle.adobe_dir)
            self.photoshop = controller
        result = controller.process_image(
            source,
            destination,
            aspect=aspect,
            width_px=target_width,
            height_px=target_height,
            dpi=int(min_dpi),
            mode=mode,  # type: ignore[arg-type]
        )

        with handle.uow() as uow:
            row = uow.assets.get(asset_id)
            if row is not None:
                row.processed = True
                row.processed_path = str(destination)
                meta = row.meta
                meta["processing"] = result
                meta["frame_mm"] = [frame_width_mm, frame_height_mm]
                row.set_meta(meta)
        log.info(
            "Processed asset %s for a %.0fx%.0f mm frame with the %s engine",
            asset_id,
            frame_width_mm,
            frame_height_mm,
            result.get("engine"),
        )
        return result

    # ----------------------------------------------------------- reporting
    def quality_map(self, handle: ProjectHandle) -> dict[int, float]:
        """``asset id -> quality score`` for the layout scorer."""
        with handle.uow() as uow:
            return {a.id: a.quality_score for a in uow.assets.for_project(handle.project_id)}

    def pixel_map(self, handle: ProjectHandle) -> dict[int, tuple[int, int]]:
        """``asset id -> (width, height)`` for the resolution constraint."""
        with handle.uow() as uow:
            return {a.id: (a.width, a.height) for a in uow.assets.for_project(handle.project_id)}

    def resolution_warnings(
        self, handle: ProjectHandle, placements: dict[int, float], min_dpi: float = 200.0
    ) -> list[str]:
        """Warn about pictures that would print below *min_dpi*."""
        warnings: list[str] = []
        with handle.uow() as uow:
            for asset_id, width_mm in placements.items():
                row = uow.assets.get(asset_id)
                if row is None or width_mm <= 0:
                    continue
                dpi = effective_dpi(row.width, width_mm)
                if dpi < min_dpi:
                    warnings.append(
                        f"{row.filename} would print at {dpi:.0f} dpi in a {width_mm:.0f} mm frame"
                    )
        return warnings

    def summary(self, handle: ProjectHandle) -> dict[str, Any]:
        """Counts for the Assets page."""
        with handle.uow() as uow:
            assets = uow.assets.for_project(handle.project_id)
            return {
                "total": len(assets),
                "generated": sum(1 for a in assets if a.ai_generated),
                "processed": sum(1 for a in assets if a.processed),
                "duplicates": sum(1 for a in assets if a.duplicate_of is not None),
                "assigned": sum(1 for a in assets if a.article_id is not None),
                "average_quality": round(sum(a.quality_score for a in assets) / len(assets), 1)
                if assets
                else 0.0,
                "low_quality": [a.filename for a in assets if self._is_weak(a)][:10],
            }

    @staticmethod
    def _is_weak(asset: E.Asset) -> bool:
        """Whether a picture must not be used for a lead position.

        A low score is one signal; a measured defect (too few pixels, blurred,
        badly exposed) is another, and either is enough to keep the picture out
        of the main slot.
        """
        if asset.quality_score < 45:
            return True
        problems = set(asset.meta.get("analysis", {}).get("problems") or [])
        blocking = {"low_resolution", "blurry", "underexposed", "overexposed", "flat_contrast"}
        return bool(problems & blocking)

    def suggested_aspect(self, width_mm: float, height_mm: float) -> str:
        """Aspect ratio label for a frame, used when generating images."""
        return closest_aspect_ratio(width_mm, max(1e-6, height_mm))

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)
