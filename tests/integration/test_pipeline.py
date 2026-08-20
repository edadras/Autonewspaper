"""The autonomous pipeline, the QA loop, export and crash recovery."""

from __future__ import annotations

import threading
from pathlib import Path

from app.core.jobs import CancelToken
from app.models.schemas import ApprovalRequest, LayoutPlan, PipelineStage, ProjectSpec


def test_a_full_run_produces_pages_previews_and_pdfs(application, project):
    result = application.pipeline.run(project, mode="auto")

    assert result.success, result.errors
    assert result.stage_reached is PipelineStage.DONE
    assert result.pdf_paths, "a run must always produce a PDF"
    for path in result.pdf_paths:
        assert Path(path).exists() and Path(path).stat().st_size > 1000
    assert result.preview_paths
    assert result.score > 0

    plan = LayoutPlan.load(project.layout_plan_path)
    assert plan.pages
    assert plan.element_count() > 10
    assert project.editorial_plan_path.exists()


def test_the_run_is_recorded_and_the_project_state_advances(application, project):
    application.pipeline.run(project, mode="auto")
    with project.uow() as uow:
        run = uow.runs.latest(project.project_id)
        assert run is not None
        assert run.status == "completed"
        assert PipelineStage.EXPORT.value in run.stages_done
        assert uow.projects.get(project.project_id).status == "generated"
        pages = uow.pages.for_project(project.project_id)
        assert pages
        assert all(uow.elements.for_page(p.id) for p in pages if p.status == "built")


def test_editorial_stage_scores_and_pages_every_story(application, project):
    application.pipeline.run(project, mode="auto")
    with project.uow() as uow:
        articles = uow.articles.for_project(project.project_id)
    assert all(a.priority > 0 for a in articles)
    assert all(a.recommended_page for a in articles)
    assert all(a.status == "planned" for a in articles)
    assert any(a.summary for a in articles)


def test_the_imported_body_is_never_rewritten(application, project):
    with project.uow() as uow:
        before = {a.id: a.body for a in uow.articles.for_project(project.project_id)}
    application.pipeline.run(project, mode="auto")
    with project.uow() as uow:
        after = {a.id: a.body for a in uow.articles.for_project(project.project_id)}
    assert before == after


def test_an_approved_story_keeps_its_headline(application, project):
    with project.uow() as uow:
        article = uow.articles.for_project(project.project_id)[0]
        article.title = "تیتر دستی سردبیر"
        article.approved = True
        article_id = article.id
    application.pipeline.run(project, mode="auto")
    with project.uow() as uow:
        assert uow.articles.get(article_id).title == "تیتر دستی سردبیر"


def test_a_run_without_content_fails_cleanly(application):
    handle = application.create_project(ProjectSpec(name="Empty", page_count=2))
    result = application.pipeline.run(handle, mode="auto")
    assert not result.success
    assert result.errors
    assert "no articles" in result.errors[0]["message"].lower()


def test_semi_automatic_mode_asks_for_approval(application, project):
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> bool:
        requests.append(request)
        return True

    result = application.pipeline.run(project, mode="semi_auto", approval=approve)
    assert result.success
    stages = {request.stage for request in requests}
    assert PipelineStage.LAYOUT in stages
    assert PipelineStage.EXPORT in stages


def test_declining_an_approval_stops_the_run(application, project):
    result = application.pipeline.run(project, mode="semi_auto", approval=lambda request: False)
    assert not result.success
    with project.uow() as uow:
        assert uow.runs.latest(project.project_id).status == "cancelled"


def test_cancelling_stops_the_run_and_leaves_the_project_usable(application, project):
    token = CancelToken()

    def cancel_soon() -> None:
        token.cancel()

    threading.Timer(0.35, cancel_soon).start()
    result = application.pipeline.run(project, mode="auto", token=token)
    assert not result.success
    with project.uow() as uow:
        assert uow.runs.latest(project.project_id).status in ("cancelled", "completed")


def test_an_interrupted_run_is_offered_for_resume(application, project):
    with project.uow() as uow:
        from app.models import entities as E

        uow.runs.add(E.PipelineRun(project_id=project.project_id, status="running", stage="layout"))
    resumable = application.projects.resumable(project)
    assert resumable is not None and resumable["stage"] == "layout"

    application.projects.mark_interrupted_runs(project)
    with project.uow() as uow:
        assert uow.runs.latest(project.project_id).status == "interrupted"


def test_resuming_skips_the_completed_stages(application, project):
    application.pipeline.run(project, mode="auto")
    result = application.pipeline.run(project, mode="auto", resume_from=PipelineStage.EXPORT)
    assert result.success
    assert result.pdf_paths


def test_qa_reports_are_produced_for_every_page(application, project):
    application.pipeline.run(project, mode="auto")
    plan = LayoutPlan.load(project.layout_plan_path)
    for page in plan.pages:
        if page.meta.get("empty"):
            continue
        assert page.qa_score > 0
        assert page.iterations >= 1
        assert "qa" in page.meta


def test_the_correction_loop_is_bounded(application, project):
    application.settings.update(layout={"qa_threshold": 100.0, "max_iterations": 2})
    application.pipeline.run(project, mode="auto")
    plan = LayoutPlan.load(project.layout_plan_path)
    for page in plan.pages:
        assert page.iterations <= 2, "the loop must respect max_iterations"


def test_thin_copy_is_concentrated_rather_than_spread(application, sample_articles_file, sample_images):
    handle = application.create_project(ProjectSpec(name="Thin", page_count=8, language="fa"))
    application.content.import_files(handle, [sample_articles_file])
    application.assets.import_files(handle, sample_images)
    result = application.pipeline.run(handle, mode="auto")

    plan = LayoutPlan.load(handle.layout_plan_path)
    filled = [p for p in plan.pages if not p.meta.get("empty")]
    assert len(plan.pages) == 8, "the requested page count is still honoured"
    assert len(filled) < 8, "thin copy must not be smeared over every page"
    assert any("fills about" in warning for warning in result.warnings)


def test_the_export_states_which_engine_produced_the_pdf(application, project):
    result = application.pipeline.run(project, mode="auto")
    assert result.adobe_strategy
    if result.adobe_strategy == "builtin-renderer":
        assert any("built-in renderer" in w for w in result.warnings)


def test_export_service_writes_the_requested_presets(application, project):
    application.pipeline.run(project, mode="auto")
    plan = LayoutPlan.load(project.layout_plan_path)
    template = application.templates.get(plan.template_id)
    result = application.exporter.export(
        project, plan, template, presets=["print", "web"], export_previews=True
    )
    assert set(result.pdfs) >= {"print", "web"}
    for path in result.pdfs.values():
        assert Path(path).exists()
    assert result.previews


def test_a_version_snapshot_is_taken_after_the_run(application, project):
    application.pipeline.run(project, mode="auto")
    versions = project.versions.list()
    assert versions
    assert (versions[-1].path / "layout_plan.json").exists()
