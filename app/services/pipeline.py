"""The autonomous pipeline.

One operator action - "Generate Newspaper" - runs every stage of
specification §23 from imported copy to a finished PDF. The pipeline:

* persists its progress after every stage so a crash can be resumed (§30);
* runs independent work in parallel and everything that touches the shared
  InDesign document sequentially (§28);
* pauses for approval in semi-automatic mode (§24, §53);
* never lets one failing stage abort the run - it records the failure, keeps
  the best result and carries on (§32).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.adobe.service import AdobeService
from app.ai.registry import AIService
from app.config.settings import SettingsManager
from app.core.errors import AppError, Component, ErrorReport, PipelineAbort, to_report
from app.core.events import EventBus, EventType
from app.core.jobs import CancelToken, JobCancelled, JobLane, JobQueue
from app.export.exporter import ExportResult, ExportService
from app.layout.engine import LayoutEngine, make_image_slot
from app.layout.strategies import ArticleBlock
from app.models import entities as E  # noqa: N812
from app.models.schemas import (
    ApprovalRequest,
    AreaKind,
    EditorialPlan,
    LayoutPlan,
    PipelineResult,
    PipelineStage,
    QAReport,
)
from app.services.asset_manager import AssetManager
from app.services.project_manager import ProjectHandle, ProjectManager
from app.templates.manager import TemplateManager
from app.templates.schema import TemplateSpec
from app.utils import text as T
from app.vision.qa_agent import VisionQAAgent
from app.vision.renderer import PreviewRenderer

log = logging.getLogger(__name__)

ApprovalCallback = Callable[[ApprovalRequest], bool]
ProgressCallback = Callable[[PipelineStage, float, str], None]


@dataclass
class PipelineContext:
    """State carried from stage to stage."""

    handle: ProjectHandle
    template: TemplateSpec
    run_id: int
    mode: str
    token: CancelToken
    editorial: EditorialPlan | None = None
    plan: LayoutPlan | None = None
    qa_reports: list[QAReport] = field(default_factory=list)
    export: ExportResult | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[ErrorReport] = field(default_factory=list)
    adobe_strategy: str = "none"
    started: float = field(default_factory=time.monotonic)
    stages_done: list[PipelineStage] = field(default_factory=list)

    def warn(self, message: str) -> None:
        """Record a non-fatal problem."""
        self.warnings.append(message)
        log.warning("%s", message)


class Pipeline:
    """Runs the full generation pipeline for one project."""

    def __init__(
        self,
        settings: SettingsManager,
        projects: ProjectManager,
        templates: TemplateManager,
        assets: AssetManager,
        ai: AIService,
        adobe: AdobeService,
        exporter: ExportService,
        jobs: JobQueue,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.projects = projects
        self.templates = templates
        self.assets = assets
        self.ai = ai
        self.adobe = adobe
        self.exporter = exporter
        self.jobs = jobs
        self.bus = bus

    # ---------------------------------------------------------------- run
    def run(
        self,
        handle: ProjectHandle,
        *,
        mode: str | None = None,
        token: CancelToken | None = None,
        approval: ApprovalCallback | None = None,
        progress: ProgressCallback | None = None,
        resume_from: PipelineStage | None = None,
    ) -> PipelineResult:
        """Execute the pipeline and return its result."""
        config = self.settings.settings
        mode = mode or config.pipeline.mode
        token = token or CancelToken()
        template = self.templates.get_or_default(handle.project()["template_id"])

        run_id = self._start_run(handle, mode)
        context = PipelineContext(handle=handle, template=template, run_id=run_id, mode=mode, token=token)
        result = PipelineResult(project_id=handle.project_id)
        self.bus.publish(
            EventType.PIPELINE_STARTED,
            slug=handle.slug,
            mode=mode,
            run_id=run_id,
            template=template.id,
        )

        stages: list[tuple[PipelineStage, Callable[[PipelineContext], None]]] = [
            (PipelineStage.IMPORT, self._stage_import),
            (PipelineStage.ANALYZE, self._stage_analyze),
            (PipelineStage.EDITORIAL, self._stage_editorial),
            (PipelineStage.ASSETS, self._stage_assets),
            (PipelineStage.IMAGE_GENERATION, self._stage_image_generation),
            (PipelineStage.LAYOUT, self._stage_layout),
            (PipelineStage.SCORING, self._stage_scoring),
            (PipelineStage.IMAGE_PROCESSING, self._stage_image_processing),
            (PipelineStage.INDESIGN, self._stage_indesign),
            (PipelineStage.QA, self._stage_qa),
            (PipelineStage.EXPORT, self._stage_export),
            (PipelineStage.ARCHIVE, self._stage_archive),
        ]
        order = [stage for stage, _ in stages]
        skip_until = order.index(resume_from) if resume_from in order else 0
        if skip_until:
            skip_until = self._restore_for_resume(context, order, skip_until)

        try:
            for index, (stage, handler) in enumerate(stages):
                if index < skip_until:
                    log.info("Resuming: skipping stage '%s'", stage.value)
                    continue
                token.raise_if_cancelled()
                self._enter_stage(context, stage, index / len(stages))
                if progress:
                    progress(stage, index / len(stages), stage.value)
                if not self._approve(context, stage, approval):
                    raise PipelineAbort(f"The operator stopped the run before '{stage.value}'")
                try:
                    handler(context)
                except (PipelineAbort, JobCancelled):
                    raise
                except Exception as exc:  # noqa: BLE001 - one stage never kills the run
                    report = to_report(exc, Component.PIPELINE)
                    context.errors.append(report)
                    log.error("Stage '%s' failed: %s", stage.value, report.summary(), exc_info=exc)
                    self.bus.publish(EventType.ERROR, stage=stage.value, error=report.to_dict())
                    if config.pipeline.stop_on_first_error or stage in _CRITICAL_STAGES:
                        result.stage_reached = stage
                        raise
                context.stages_done.append(stage)
                self._record_stage(context, stage)
            result.stage_reached = PipelineStage.DONE
            result.success = True
        except (PipelineAbort, JobCancelled) as abort:
            message = getattr(abort, "message", None) or str(abort) or "The run was cancelled"
            abort = PipelineAbort(message)
            context.warn(message)
            self._finish_run(context, "cancelled")
            self.bus.publish(EventType.PIPELINE_CANCELLED, slug=handle.slug, reason=abort.message)
            result.stage_reached = context.stages_done[-1] if context.stages_done else PipelineStage.IMPORT
            result.warnings = context.warnings
            result.errors = [e.to_dict() for e in context.errors]
            result.duration_seconds = time.monotonic() - context.started
            return result
        except Exception as exc:  # noqa: BLE001
            report = to_report(exc, Component.PIPELINE)
            context.errors.append(report)
            if token.cancelled:
                # The failure is a consequence of the operator stopping the run.
                self._finish_run(context, "cancelled")
                self.bus.publish(EventType.PIPELINE_CANCELLED, slug=handle.slug, reason=report.message)
                result.success = False
                result.warnings = context.warnings
                result.errors = [e.to_dict() for e in context.errors]
                result.duration_seconds = time.monotonic() - context.started
                return result
            self._finish_run(context, "failed")
            self.bus.publish(EventType.PIPELINE_FAILED, slug=handle.slug, error=report.to_dict())
            result.success = False
            result.warnings = context.warnings
            result.errors = [e.to_dict() for e in context.errors]
            result.duration_seconds = time.monotonic() - context.started
            return result

        self._populate_result(context, result)
        self._finish_run(context, "completed", result)
        self.bus.publish(EventType.PIPELINE_FINISHED, slug=handle.slug, result=result.model_dump(mode="json"))
        log.info(result.summary())
        return result

    def _restore_for_resume(self, ctx: PipelineContext, order: list[PipelineStage], skip_until: int) -> int:
        """Load the artefacts the skipped stages would have produced.

        A resumed run must not start from a later stage with an empty context;
        the editorial and layout plans are read back from the project folder.
        When the layout plan is missing the run restarts from the layout stage
        rather than failing.
        """
        if ctx.handle.editorial_plan_path.exists():
            try:
                ctx.editorial = EditorialPlan.load(ctx.handle.editorial_plan_path)
            except Exception as exc:  # noqa: BLE001
                ctx.warn(f"The stored editorial plan could not be read: {exc}")

        needs_plan = order[skip_until] not in (
            PipelineStage.IMPORT,
            PipelineStage.ANALYZE,
            PipelineStage.EDITORIAL,
            PipelineStage.ASSETS,
            PipelineStage.IMAGE_GENERATION,
        )
        if not needs_plan:
            return skip_until
        if ctx.handle.layout_plan_path.exists():
            try:
                ctx.plan = LayoutPlan.load(ctx.handle.layout_plan_path)
                log.info("Resuming with the stored layout plan (%d page(s))", len(ctx.plan.pages))
                return skip_until
            except Exception as exc:  # noqa: BLE001
                ctx.warn(f"The stored layout plan could not be read: {exc}")
        ctx.warn("No usable layout plan was found for the resume; restarting from the layout stage.")
        return order.index(PipelineStage.LAYOUT)

    # ------------------------------------------------------------- stages
    def _stage_import(self, ctx: PipelineContext) -> None:
        """Validate that there is something to lay out."""
        with ctx.handle.uow() as uow:
            articles = uow.articles.for_project(ctx.handle.project_id)
        if not articles:
            raise AppError(
                "The project has no articles",
                component=Component.PIPELINE,
                recovery_action="Import content on the Content page before generating.",
            )
        empty = [a.id for a in articles if not a.body.strip()]
        if empty:
            ctx.warn(f"{len(empty)} article(s) have no body text and will be laid out as briefs")
        log.info("Stage import: %d article(s) ready", len(articles))

    def _stage_analyze(self, ctx: PipelineContext) -> None:
        """Measure every picture (parallel; independent work)."""
        job = self.jobs.submit(
            "Analyse images",
            lambda job_ctx: self.assets.analyze_all(ctx.handle, job_ctx),
            lane=JobLane.PARALLEL,
        )
        self.jobs.wait([job], timeout=600)
        count = job.result or 0
        summary = self.assets.summary(ctx.handle)
        if summary["low_quality"]:
            ctx.warn(
                "Low quality image(s) will not be used for lead slots: "
                + ", ".join(summary["low_quality"][:5])
            )
        log.info("Stage analyze: %s image(s) measured", count)

    def _stage_editorial(self, ctx: PipelineContext) -> None:
        """Score the copy, write the display type and assign pages."""
        project = ctx.handle.project()
        with ctx.handle.uow() as uow:
            articles = uow.articles.for_project(ctx.handle.project_id)
            payload = [
                {
                    "id": article.id,
                    "title": article.display_title,
                    "body": article.body[:6000],
                    "category": article.category,
                    "author": article.author,
                    "source": article.source,
                    "word_count": article.word_count,
                    "page_preference": article.page_preference,
                    "has_image": bool(uow.assets.for_article(article.id)),
                }
                for article in articles
            ]

        requested_pages = int(project["page_count"])
        usable_pages = self._effective_page_count(ctx, payload, requested_pages)
        if usable_pages < requested_pages:
            ctx.warn(
                f"The imported copy fills about {usable_pages} of the {requested_pages} requested "
                f"page(s); the remaining page(s) are left empty - add content or reduce the page count."
            )

        plan = self.ai.analyze_articles(
            payload,
            project_id=ctx.handle.project_id,
            publication_name=project["publication_name"],
            edition_date=str(project["edition_date"] or ""),
            language=project["language"],
            page_count=usable_pages,
            design_style=project["design_style"],
        )
        ctx.editorial = plan
        plan.save(ctx.handle.editorial_plan_path)
        self._apply_editorial(ctx, plan)
        log.info(
            "Stage editorial: %d article(s) scored by %s/%s",
            len(plan.analyses),
            plan.provider,
            plan.model,
        )

    def _effective_page_count(
        self, ctx: PipelineContext, articles: list[dict[str, Any]], requested: int
    ) -> int:
        """How many pages the imported copy can actually fill.

        Spreading thin content over every requested page produces a set of
        half-empty pages that QA then - correctly - marks down. Concentrating
        it on the pages it can fill and reporting the surplus is what an editor
        would do, and leaves the operator a clear decision.
        """
        from app.layout.typography import TypographyEngine

        typography = TypographyEngine(ctx.template, ctx.template.language)
        page_area = ctx.template.content_width_mm * ctx.template.content_height_mm
        # Display type, pictures, captions and gutters take roughly a third of
        # a news page; the rest is running text.
        text_area = page_area * 0.62
        capacity = text_area * typography.words_per_mm2()
        if capacity <= 0:
            return requested
        demand = float(sum(int(a.get("word_count") or 0) for a in articles))
        if demand <= 0:
            return 1
        needed = int(-(-demand // capacity))
        return max(1, min(requested, needed))

    def _apply_editorial(self, ctx: PipelineContext, plan: EditorialPlan) -> None:
        """Write the editorial decisions back into the database.

        The original body is never replaced (specification §8): only the
        headline, deck, lead and summary are AI-editable, and even those are
        kept out of the way of a manual edit the operator has approved.
        """
        with ctx.handle.uow() as uow:
            for analysis in plan.analyses:
                article = uow.articles.get(analysis.article_id)
                if article is None:
                    continue
                if not article.approved:
                    if analysis.headline:
                        article.original_title = article.original_title or article.title
                        article.title = T.clean_headline(analysis.headline, article.language)
                    if analysis.subtitle:
                        article.subtitle = analysis.subtitle
                    if analysis.lead:
                        article.lead = analysis.lead
                    if analysis.summary:
                        article.summary = analysis.summary
                article.importance = analysis.importance
                article.urgency = analysis.urgency
                article.public_interest = analysis.public_interest
                article.visual_importance = analysis.visual_importance
                article.priority = analysis.priority
                article.category = analysis.category or article.category
                article.recommended_page = analysis.recommended_page
                article.recommended_area = analysis.recommended_area.value
                article.image_required = article.image_required or analysis.image_required
                meta = article.meta
                meta.update(
                    {
                        "keywords": analysis.keywords,
                        "rationale": analysis.rationale,
                        "ai_image_prompt": analysis.ai_image_prompt,
                        "editorial_provider": plan.provider,
                    }
                )
                article.set_meta(meta)
                article.status = "planned"

    def _stage_assets(self, ctx: PipelineContext) -> None:
        """Give the available pictures to the stories that need them."""
        assigned = self.assets.auto_assign(ctx.handle)
        log.info("Stage assets: %d picture(s) assigned", assigned)

    def _stage_image_generation(self, ctx: PipelineContext) -> None:
        """Generate the missing pictures."""
        if not self.settings.settings.pipeline.generate_missing_images:
            log.info("Stage image generation: disabled in settings")
            return
        provider = self.settings.settings.image_ai.provider
        project = ctx.handle.project()
        job = self.jobs.submit(
            "Generate images",
            lambda job_ctx: self.assets.generate_missing(
                ctx.handle,
                design_style=project["design_style"],
                language=project["language"],
                ctx=job_ctx,
            ),
            lane=JobLane.PARALLEL,
        )
        self.jobs.wait([job], timeout=1800)
        created = job.result or []
        if provider == "none" and created:
            ctx.warn(
                f"{len(created)} frame(s) were filled with a marked placeholder because image "
                "generation is disabled; replace them before printing."
            )
        log.info("Stage image generation: %d image(s) created via '%s'", len(created), provider)

    def _stage_layout(self, ctx: PipelineContext) -> None:
        """Build the layout plan."""
        project = ctx.handle.project()
        config = self.settings.settings.layout
        engine = LayoutEngine(
            ctx.template,
            language=project["language"],
            candidates_per_page=config.candidates_per_page,
            seed=config.random_seed,
        )
        blocks = self._blocks_by_page(ctx)
        sections = self._sections(ctx, blocks)
        plan = engine.plan_edition(
            ctx.handle.project_id,
            blocks,
            page_count=int(project["page_count"]),
            project_slug=ctx.handle.slug,
            design_style=project["design_style"],
            publication_name=project["publication_name"],
            edition_date=str(project["edition_date"] or ""),
            sections=sections,
            asset_quality=self.assets.quality_map(ctx.handle),
            asset_pixels=self.assets.pixel_map(ctx.handle),
        )
        ctx.plan = plan
        plan.save(ctx.handle.layout_plan_path)
        self._persist_plan(ctx, plan)
        unplaced = plan.meta.get("unplaced_articles") or []
        if unplaced:
            ctx.warn(f"{len(unplaced)} story/stories did not fit the edition; add pages or shorten copy.")
        self.bus.publish(
            EventType.LAYOUT_PLANNED,
            slug=ctx.handle.slug,
            pages=len(plan.pages),
            elements=plan.element_count(),
            score=plan.score,
        )
        log.info(
            "Stage layout: %d page(s), %d frame(s), score %.1f",
            len(plan.pages),
            plan.element_count(),
            plan.score,
        )

    def _blocks_by_page(self, ctx: PipelineContext) -> dict[int, list[ArticleBlock]]:
        """Convert the database rows into layout blocks, grouped by page."""
        blocks: dict[int, list[ArticleBlock]] = {}
        with ctx.handle.uow() as uow:
            articles = uow.articles.by_priority(ctx.handle.project_id)
            for article in articles:
                page = article.recommended_page or article.page_preference or 1
                asset = None
                candidates = [a for a in uow.assets.for_article(article.id) if a.duplicate_of is None]
                if candidates:
                    asset = max(candidates, key=lambda a: (a.quality_score, a.width * a.height))
                area = _area_from(article.recommended_area)
                image = None
                if asset is not None:
                    image = make_image_slot(
                        asset.id,
                        asset.usable_path,
                        asset.width,
                        asset.height,
                        asset.quality_score,
                        caption=asset.caption or _caption_for(article, asset),
                        generated=asset.ai_generated,
                    )
                blocks.setdefault(page, []).append(
                    ArticleBlock(
                        article_id=article.id,
                        headline=article.display_title,
                        subtitle=article.subtitle,
                        kicker=_kicker_for(article),
                        byline=article.author,
                        lead=article.lead or T.lead_paragraph(article.body, 32),
                        body=article.body,
                        quote=article.meta.get("quote", ""),
                        weight=max(0.2, article.priority / 100.0),
                        area=area,
                        priority=article.priority,
                        image=image,
                        meta={"category": article.category},
                    )
                )
        return blocks

    def _sections(self, ctx: PipelineContext, blocks: dict[int, list[ArticleBlock]]) -> dict[int, str]:
        """Name each page after the category that dominates it."""
        sections: dict[int, str] = {}
        language = ctx.handle.project()["language"]
        for page, items in blocks.items():
            categories = [b.meta.get("category", "") for b in items if b.meta.get("category")]
            if categories:
                dominant = max(set(categories), key=categories.count)
                sections[page] = _section_label(dominant, language)
        return sections

    def _persist_plan(self, ctx: PipelineContext, plan: LayoutPlan) -> None:
        """Write the plan into the pages/elements tables."""
        with ctx.handle.uow() as uow:
            uow.pages.clear(ctx.handle.project_id)
            for page in plan.pages:
                row = E.Page(
                    project_id=ctx.handle.project_id,
                    index=page.index,
                    section=page.section,
                    master=page.master,
                    width_mm=page.width_mm,
                    height_mm=page.height_mm,
                    columns=page.columns,
                    score=page.score,
                    qa_score=page.qa_score,
                    iterations=page.iterations,
                    preview_path=str(page.meta.get("preview", "")),
                    status="empty" if page.meta.get("empty") else ("built" if page.qa_score else "planned"),
                )
                row.set_meta(page.meta)
                uow.pages.add(row)
                for element in page.elements:
                    item = E.LayoutElement(
                        page_id=row.id,
                        type=element.type.value,
                        x=element.rect.x,
                        y=element.rect.y,
                        width=element.rect.width,
                        height=element.rect.height,
                        z_index=element.z_index,
                        rotation=element.rotation,
                        column_span=element.column_span,
                        article_id=element.article_id,
                        asset_id=element.asset_id,
                        style_id=element.style_id,
                        frame_name=element.frame_name,
                        text_content=element.text,
                        image_path=element.image_path or "",
                        overflow=element.estimated_overflow > 0.001,
                        locked=element.locked,
                    )
                    item.set_meta(element.meta)
                    uow.elements.add(item)

    def _stage_scoring(self, ctx: PipelineContext) -> None:
        """Report the plan's score (the engine already picked the best)."""
        if ctx.plan is None:
            return
        worst = sorted(ctx.plan.pages, key=lambda p: p.score)[:3]
        for page in worst:
            if page.meta.get("empty"):
                ctx.warn(f"Page {page.index} is empty; consider reducing the page count")
            elif page.score < self.settings.settings.layout.qa_threshold - 15:
                ctx.warn(f"Page {page.index} scored only {page.score:.1f} before QA")
        self.bus.publish(
            EventType.LAYOUT_SCORED,
            slug=ctx.handle.slug,
            score=ctx.plan.score,
            pages={p.index: p.score for p in ctx.plan.pages},
        )
        log.info("Stage scoring: edition score %.1f", ctx.plan.score)

    def _stage_image_processing(self, ctx: PipelineContext) -> None:
        """Crop and resample each placed picture to the frame it will occupy."""
        if ctx.plan is None:
            return
        tasks: list[tuple[int, float, float]] = []
        for page in ctx.plan.pages:
            for element in page.elements:
                if element.is_image and element.asset_id:
                    tasks.append((element.asset_id, element.rect.width, element.rect.height))
        if not tasks:
            log.info("Stage image processing: nothing to process")
            return

        min_dpi = max(200.0, self.settings.settings.layout.min_image_dpi)
        jobs = self.jobs.map(
            "Process image",
            tasks,
            lambda task, _ctx: self.assets.process_for_frame(
                ctx.handle,
                task[0],
                frame_width_mm=task[1],
                frame_height_mm=task[2],
                min_dpi=min_dpi,
            ),
        )
        self.jobs.wait(jobs, timeout=1800)
        processed = sum(1 for job in jobs if job.state.value == "succeeded")
        failed = [job for job in jobs if job.state.value == "failed"]
        for job in failed:
            ctx.warn(f"Image processing failed: {job.error.message if job.error else job.name}")

        # Point the plan at the processed derivatives.
        with ctx.handle.uow() as uow:
            paths = {a.id: a.usable_path for a in uow.assets.for_project(ctx.handle.project_id)}
        for page in ctx.plan.pages:
            for element in page.elements:
                if element.is_image and element.asset_id in paths:
                    element.image_path = paths[element.asset_id]
        ctx.plan.save(ctx.handle.layout_plan_path)
        log.info("Stage image processing: %d/%d picture(s) processed", processed, len(tasks))

    def _stage_indesign(self, ctx: PipelineContext) -> None:
        """Build the document in InDesign (exclusive: one shared document)."""
        if ctx.plan is None:
            raise AppError(
                "There is no layout plan to build",
                component=Component.INDESIGN,
                recovery_action="Run the layout stage first.",
            )
        if not self.adobe.indesign_app.installed:
            ctx.adobe_strategy = "builtin-renderer"
            ctx.warn(
                "InDesign is not installed on this machine; the layout, previews and PDF are "
                "produced by the built-in renderer."
            )
            return

        controller = self.adobe.indesign
        job = self.jobs.submit(
            "Build the InDesign document",
            lambda _ctx: controller.build_document(ctx.plan, ctx.template),  # type: ignore[arg-type]
            lane=JobLane.EXCLUSIVE,
        )
        self.jobs.wait([job], timeout=self.settings.settings.adobe.script_timeout_seconds + 120)
        if job.state.value != "succeeded":
            ctx.adobe_strategy = "builtin-renderer"
            ctx.warn(
                "InDesign could not build the document "
                f"({job.error.message if job.error else 'unknown error'}); "
                "the built-in renderer is used for previews and the PDF."
            )
            return
        data = job.result or {}
        ctx.adobe_strategy = str(data.get("strategy") or "com")
        overflow = data.get("overflow") or []
        if overflow:
            ctx.warn(f"InDesign reports {len(overflow)} overflowing frame(s) before QA")
        log.info(
            "Stage InDesign: document built via '%s' with %d page(s)",
            ctx.adobe_strategy,
            len(ctx.plan.pages),
        )

    def _stage_qa(self, ctx: PipelineContext) -> None:
        """Render, review and correct every page (specification §16)."""
        if ctx.plan is None:
            raise AppError(
                "There is no layout plan to review",
                component=Component.PIPELINE,
                recovery_action="Run the layout stage first.",
            )
        project = ctx.handle.project()
        config = self.settings.settings
        engine = LayoutEngine(
            ctx.template,
            language=project["language"],
            candidates_per_page=config.layout.candidates_per_page,
            seed=config.layout.random_seed,
        )
        agent = VisionQAAgent(
            engine,
            ctx.template,
            ai=self.ai,
            threshold=config.layout.qa_threshold,
            max_iterations=config.layout.max_iterations,
            use_vision_model=config.ai.vision_provider != "heuristic",
            preview_dpi=config.export.preview_dpi,
        )
        renderer = PreviewRenderer(ctx.template, dpi=config.export.preview_dpi)
        use_indesign = ctx.adobe_strategy not in ("builtin-renderer", "none")
        controller = self.adobe.indesign if use_indesign else None
        quality = self.assets.quality_map(ctx.handle)
        pixels = self.assets.pixel_map(ctx.handle)

        corrected_pages = []
        for page in ctx.plan.pages:
            ctx.token.raise_if_cancelled()
            attempt = {"n": 0}

            def render(target_page, _attempt=attempt, _controller=controller):
                _attempt["n"] += 1
                preview = ctx.handle.previews_dir / f"page_{target_page.index:03d}_it{_attempt['n']}.png"
                report = None
                if _controller is not None:
                    try:
                        _controller.render_preview(target_page.index, preview, dpi=config.export.preview_dpi)
                        report = _controller.page_report(target_page.index)
                        return (preview, report)
                    except Exception as exc:  # noqa: BLE001
                        ctx.warn(
                            f"InDesign preview for page {target_page.index} failed ({exc}); "
                            "the built-in renderer is used for QA of this page."
                        )
                renderer.render_page(target_page, preview)
                return (preview, report)

            loop = agent.run_loop(page, render, asset_quality=quality, asset_pixels=pixels)
            corrected_pages.append(loop.page)
            ctx.qa_reports.append(loop.report)
            self.bus.publish(
                EventType.QA_REPORT,
                slug=ctx.handle.slug,
                page=page.index,
                score=loop.report.score,
                passed=loop.passed,
                issues=len(loop.report.issues),
            )
            if page.meta.get("empty"):
                continue
            if not loop.passed:
                ctx.warn(
                    f"Page {page.index} kept at score {loop.report.score:.1f} "
                    f"after {len(loop.iterations)} iteration(s)"
                )
            if loop.report.preview_path:
                self.bus.publish(EventType.PREVIEW_READY, page=page.index, path=loop.report.preview_path)

        rebuilt = [
            page
            for page, original in zip(corrected_pages, ctx.plan.pages, strict=False)
            if page.elements != original.elements
        ]
        ctx.plan.pages = corrected_pages
        # Pages left empty because the edition ran out of copy are reported as
        # a warning; averaging their score would hide the quality of the rest.
        scored = [p for p in corrected_pages if not p.meta.get("empty") and p.elements]
        ctx.plan.score = round(sum(p.qa_score for p in scored) / len(scored), 2) if scored else 0.0
        ctx.plan.save(ctx.handle.layout_plan_path)
        self._persist_plan(ctx, ctx.plan)

        if rebuilt and use_indesign:
            self._rebuild_corrected_pages(ctx, rebuilt)
        log.info("Stage QA: edition score %.1f over %d page(s)", ctx.plan.score, len(corrected_pages))

    def _rebuild_corrected_pages(self, ctx: PipelineContext, pages: list[Any]) -> None:
        """Re-apply corrected pages to the InDesign document."""
        controller = self.adobe.indesign
        job = self.jobs.submit(
            "Apply corrections in InDesign",
            lambda _ctx: [controller.build_page(page, ctx.template) for page in pages],
            lane=JobLane.EXCLUSIVE,
        )
        self.jobs.wait([job], timeout=self.settings.settings.adobe.script_timeout_seconds)
        if job.state.value != "succeeded":
            ctx.warn(
                "Corrected pages could not be re-applied to the InDesign document "
                f"({job.error.message if job.error else 'unknown error'})"
            )
        else:
            self.bus.publish(
                EventType.LAYOUT_CORRECTED,
                slug=ctx.handle.slug,
                pages=[p.index for p in pages],
            )

    def _stage_export(self, ctx: PipelineContext) -> None:
        """Produce the deliverables."""
        if ctx.plan is None:
            raise AppError(
                "There is no layout plan to export",
                component=Component.EXPORT,
                recovery_action="Run the layout stage before exporting.",
            )
        config = self.settings.settings.export
        presets = [config.default_preset]
        if config.default_preset != "digital":
            presets.append("digital")
        exporter = ExportService(
            self.adobe.indesign if ctx.adobe_strategy not in ("builtin-renderer", "none") else None,
            self.bus,
        )
        job = self.jobs.submit(
            "Export",
            lambda _ctx: exporter.export(
                ctx.handle,
                ctx.plan,
                ctx.template,
                presets=presets,
                export_indd=config.export_indd,
                export_idml=config.export_idml,
                preview_dpi=config.preview_dpi,
            ),
            lane=JobLane.EXCLUSIVE,
        )
        self.jobs.wait([job], timeout=self.settings.settings.adobe.script_timeout_seconds + 300)
        if job.state.value != "succeeded" or job.result is None:
            raise AppError(
                f"Export failed: {job.error.message if job.error else 'unknown error'}",
                component=Component.EXPORT,
            )
        ctx.export = job.result
        ctx.warnings.extend(ctx.export.warnings)
        log.info("Stage export: %s", ", ".join(ctx.export.pdfs) or "nothing produced")

    def _stage_archive(self, ctx: PipelineContext) -> None:
        """Snapshot the project and close the Adobe documents."""
        try:
            ctx.handle.versions.create(
                label=f"Generated edition (score {ctx.plan.score if ctx.plan else 0:.1f})",
                note=f"run {ctx.run_id}",
                extra={"warnings": len(ctx.warnings), "errors": len(ctx.errors)},
            )
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"Could not snapshot the project: {exc}")
        self.projects.save(ctx.handle)
        if self.settings.settings.adobe.close_documents_on_finish and self.adobe.indesign_app.installed:
            try:
                self.adobe.indesign.close_document(save=False)
            except Exception as exc:  # noqa: BLE001
                log.debug("Closing the InDesign document failed: %s", exc)

    # ----------------------------------------------------------- approvals
    def _approve(self, ctx: PipelineContext, stage: PipelineStage, approval: ApprovalCallback | None) -> bool:
        """Ask for approval before the gated stages in semi-automatic mode."""
        if ctx.mode == "auto" or approval is None:
            return True
        if stage not in _APPROVAL_STAGES:
            return True
        request = ApprovalRequest(
            stage=stage,
            title=_APPROVAL_STAGES[stage],
            description=_approval_description(ctx, stage),
            payload=_approval_payload(ctx, stage),
        )
        self.bus.publish(
            EventType.APPROVAL_REQUIRED,
            slug=ctx.handle.slug,
            stage=stage.value,
            title=request.title,
        )
        self._set_run_status(ctx, "awaiting_approval")
        approved = bool(approval(request))
        self._set_run_status(ctx, "running")
        if not approved:
            log.info("The operator declined stage '%s'", stage.value)
        return approved

    # ------------------------------------------------------------ run rows
    def _start_run(self, handle: ProjectHandle, mode: str) -> int:
        with handle.uow() as uow:
            run = E.PipelineRun(project_id=handle.project_id, mode=mode, status="running")
            uow.runs.add(run)
            return run.id

    def _enter_stage(self, ctx: PipelineContext, stage: PipelineStage, fraction: float) -> None:
        with ctx.handle.uow() as uow:
            run = uow.runs.get(ctx.run_id)
            if run is not None:
                run.stage = stage.value
        self.bus.publish(EventType.PIPELINE_STAGE, slug=ctx.handle.slug, stage=stage.value, progress=fraction)

    def _record_stage(self, ctx: PipelineContext, stage: PipelineStage) -> None:
        with ctx.handle.uow() as uow:
            run = uow.runs.get(ctx.run_id)
            if run is not None:
                run.mark_stage(stage.value)
                run.score = ctx.plan.score if ctx.plan else 0.0

    def _set_run_status(self, ctx: PipelineContext, status: str) -> None:
        with ctx.handle.uow() as uow:
            run = uow.runs.get(ctx.run_id)
            if run is not None:
                run.status = status

    def _finish_run(self, ctx: PipelineContext, status: str, result: PipelineResult | None = None) -> None:
        with ctx.handle.uow() as uow:
            run = uow.runs.get(ctx.run_id)
            if run is None:
                return
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.score = ctx.plan.score if ctx.plan else 0.0
            run.set_result(result.model_dump(mode="json") if result else {})
            if ctx.errors:
                run.error_json = E.JSONMixin.dump([e.to_dict() for e in ctx.errors])
            project = uow.projects.get(ctx.handle.project_id)
            if project is not None:
                project.status = {"completed": "generated", "failed": "error", "cancelled": "draft"}.get(
                    status, project.status
                )

    def _populate_result(self, ctx: PipelineContext, result: PipelineResult) -> None:
        """Fill the result object from the context."""
        result.iterations = max((r.iteration for r in ctx.qa_reports), default=0)
        result.score = ctx.plan.score if ctx.plan else 0.0
        result.adobe_strategy = ctx.adobe_strategy
        result.warnings = ctx.warnings
        result.errors = [e.to_dict() for e in ctx.errors]
        result.duration_seconds = round(time.monotonic() - ctx.started, 2)
        if ctx.export is not None:
            result.pdf_paths = list(ctx.export.pdfs.values())
            result.indd_path = ctx.export.indd
            result.idml_path = ctx.export.idml
            result.preview_paths = ctx.export.previews
            result.archive_path = ctx.export.archive


_CRITICAL_STAGES = {PipelineStage.IMPORT, PipelineStage.LAYOUT, PipelineStage.EXPORT}

_APPROVAL_STAGES = {
    PipelineStage.LAYOUT: "Approve the editorial plan",
    PipelineStage.INDESIGN: "Approve the layout",
    PipelineStage.EXPORT: "Approve the export",
}


def _approval_description(ctx: PipelineContext, stage: PipelineStage) -> str:
    if stage is PipelineStage.LAYOUT and ctx.editorial:
        return (
            f"{len(ctx.editorial.analyses)} story/stories scored; "
            f"front-page lead: {ctx.editorial.front_page_lead}"
        )
    if stage is PipelineStage.INDESIGN and ctx.plan:
        return (
            f"{len(ctx.plan.pages)} page(s), {ctx.plan.element_count()} frame(s), "
            f"planning score {ctx.plan.score:.1f}"
        )
    if stage is PipelineStage.EXPORT and ctx.plan:
        return f"Edition score after QA: {ctx.plan.score:.1f}"
    return ""


def _approval_payload(ctx: PipelineContext, stage: PipelineStage) -> dict[str, Any]:
    if stage is PipelineStage.LAYOUT and ctx.editorial:
        return {
            "analyses": [a.model_dump(mode="json") for a in ctx.editorial.analyses],
            "page_assignments": ctx.editorial.page_assignments,
        }
    if ctx.plan:
        return {
            "pages": [
                {"index": p.index, "score": p.score, "elements": len(p.elements)} for p in ctx.plan.pages
            ]
        }
    return {}


def _area_from(value: str | None) -> AreaKind:
    """Coerce a stored area name into :class:`AreaKind`."""
    try:
        return AreaKind(value or "secondary")
    except ValueError:
        return AreaKind.SECONDARY


SECTION_LABELS_FA = {
    "politics": "سیاسی",
    "economy": "اقتصادی",
    "sport": "ورزشی",
    "culture": "فرهنگی",
    "society": "جامعه",
    "world": "بین‌الملل",
    "science": "علم و فناوری",
    "incident": "حادثه",
}


def _section_label(category: str, language: str) -> str:
    """Printed section name for a category, in the edition's language."""
    key = (category or "").strip().lower()
    if not key or key == "general":
        return ""
    if language == "fa":
        return SECTION_LABELS_FA.get(key, category)
    return category.replace("_", " ").title()


def _kicker_for(article: E.Article) -> str:
    """Section label printed above the headline."""
    return _section_label(article.category, article.language)


def _caption_for(article: E.Article, asset: E.Asset) -> str:
    """Default caption when the operator has not written one.

    A picture credit is only printed when the story's source looks like an
    agency; the name of the file the copy was imported from is not a credit.
    """
    if asset.ai_generated:
        return "تصویر تولیدشده با هوش مصنوعی" if article.language == "fa" else "AI-generated illustration"
    source = (article.source or "").strip()
    if not source or Path(source).suffix or "/" in source or "\\" in source:
        return ""
    return source
