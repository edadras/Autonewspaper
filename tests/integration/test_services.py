"""Projects, content import, assets and templates working together."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.core.errors import ContentImportError
from app.models.schemas import ProjectSpec
from app.services.project_manager import SUBDIRECTORIES


def test_new_project_creates_the_documented_folder_structure(application):
    handle = application.create_project(ProjectSpec(name="Layout Test", page_count=4))
    for sub in SUBDIRECTORIES:
        assert (handle.directory / sub).is_dir(), f"missing {sub}"
    assert handle.manifest_path.exists()
    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["page_count"] == 4
    assert (handle.directory / "database.sqlite").exists()


def test_projects_are_listed_reopened_and_archived(application, tmp_path):
    handle = application.create_project(ProjectSpec(name="Reopen Me", page_count=2))
    slug = handle.slug
    assert any(row["slug"] == slug for row in application.projects.list_projects())

    application.projects.close(slug)
    reopened = application.projects.open(slug)
    assert reopened.project()["name"] == "Reopen Me"

    archive = application.projects.archive(reopened)
    assert archive.exists() and archive.stat().st_size > 0


def test_duplicating_a_project_keeps_its_content(application, sample_articles_file):
    handle = application.create_project(ProjectSpec(name="Original", page_count=2))
    application.content.import_files(handle, [sample_articles_file])
    copy = application.projects.duplicate(handle, "Copy")
    with copy.uow() as uow:
        assert len(uow.articles.for_project(copy.project_id)) == 5
    assert copy.slug != handle.slug


@pytest.mark.parametrize("suffix", [".txt", ".json", ".csv", ".html"])
def test_every_import_format_produces_articles(application, tmp_path, suffix):
    handle = application.create_project(ProjectSpec(name=f"Import {suffix}", page_count=2))
    path = tmp_path / f"content{suffix}"
    if suffix == ".txt":
        path.write_text("title: خبر یک\n\nمتن خبر اول.\n\n---\n\nخبر دو\n\nمتن خبر دوم.", encoding="utf-8")
        expected = 2
    elif suffix == ".json":
        path.write_text(
            json.dumps([{"title": "A", "body": "Body A", "category": "world", "page": 2}]),
            encoding="utf-8",
        )
        expected = 1
    elif suffix == ".csv":
        with path.open("w", encoding="utf-8", newline="") as handle_file:
            writer = csv.writer(handle_file)
            writer.writerow(["title", "body", "category", "priority"])
            writer.writerow(["CSV story", "Some body text.", "sport", "80"])
        expected = 1
    else:
        path.write_text(
            "<html><body><article><h1>HTML headline</h1><p>First.</p><p>Second.</p></article></body></html>",
            encoding="utf-8",
        )
        expected = 1

    result = application.content.import_files(handle, [path])
    assert result.count == expected, result.to_dict()
    with handle.uow() as uow:
        articles = uow.articles.for_project(handle.project_id)
        assert all(a.word_count > 0 for a in articles)


def test_import_metadata_headers_are_honoured(application, tmp_path):
    handle = application.create_project(ProjectSpec(name="Meta", page_count=3))
    path = tmp_path / "story.txt"
    path.write_text(
        "title: تیتر\ncategory: economy\nauthor: نویسنده\npage: 2\npriority: 88\n\nمتن خبر.",
        encoding="utf-8",
    )
    application.content.import_files(handle, [path])
    with handle.uow() as uow:
        article = uow.articles.for_project(handle.project_id)[0]
    assert article.category == "economy"
    assert article.author == "نویسنده"
    assert article.page_preference == 2
    assert article.priority == 88


def test_an_unreadable_file_is_skipped_not_fatal(application, tmp_path):
    handle = application.create_project(ProjectSpec(name="Bad import", page_count=1))
    good = tmp_path / "good.txt"
    good.write_text("title: ok\n\nbody text", encoding="utf-8")
    bad = tmp_path / "bad.xyz"
    bad.write_text("nonsense", encoding="utf-8")
    result = application.content.import_files(handle, [good, bad])
    assert result.count == 1
    assert result.skipped and result.skipped[0][0] == "bad.xyz"


def test_parsing_an_unsupported_format_raises_a_typed_error(application, tmp_path):
    path = tmp_path / "thing.xyz"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ContentImportError):
        application.content.parse_file(path)


def test_clipboard_import(application):
    handle = application.create_project(ProjectSpec(name="Clip", page_count=1))
    result = application.content.import_text(handle, "title: Pasted\n\nSome pasted body.")
    assert result.count == 1


def test_assets_are_measured_and_deduplicated(application, tmp_path, sample_images):
    from PIL import Image

    handle = application.create_project(ProjectSpec(name="Assets", page_count=2))
    near_duplicate = tmp_path / "copy.jpg"
    with Image.open(sample_images[0]) as image:
        image.save(near_duplicate, quality=88)

    result = application.assets.import_files(handle, [*sample_images, near_duplicate])
    assert result.count == 4
    with handle.uow() as uow:
        assets = uow.assets.for_project(handle.project_id)
    assert all(a.width > 0 and a.quality_score > 0 for a in assets)
    assert any(a.duplicate_of is not None for a in assets), "the near-duplicate must be flagged"


def test_a_low_quality_image_is_reported(application, tmp_path):
    from tests.conftest import make_image

    handle = application.create_project(ProjectSpec(name="Quality", page_count=1))
    small = make_image(tmp_path / "small.jpg", width=320, height=240)
    application.assets.import_files(handle, [small])
    summary = application.assets.summary(handle)
    assert "small.jpg" in summary["low_quality"]


def test_processing_targets_the_frame_size(application, sample_images, tmp_path):
    handle = application.create_project(ProjectSpec(name="Process", page_count=1))
    application.assets.import_files(handle, sample_images[:1])
    result = application.assets.process_for_frame(
        handle, 1, frame_width_mm=170, frame_height_mm=96, min_dpi=300
    )
    assert Path(result["path"]).exists()
    assert result["width"] == pytest.approx(170 / 25.4 * 300, rel=0.02)
    with handle.uow() as uow:
        asset = uow.assets.get(1)
    assert asset.processed and asset.processed_path


def test_auto_assign_prefers_the_best_picture(application, project):
    """Pictures are handed out after the editorial stage has set the priorities."""
    from app.agents.editorial_agent import EditorialAgent

    plan = EditorialAgent(application.ai).plan_edition(project)
    with project.uow() as uow:
        for analysis in plan.analyses:
            article = uow.articles.get(analysis.article_id)
            article.priority = analysis.priority
            article.image_required = analysis.image_required

    assigned = application.assets.auto_assign(project)
    assert assigned > 0
    with project.uow() as uow:
        linked = [a for a in uow.assets.for_project(project.project_id) if a.article_id]
    assert linked
    # The best picture must go to the highest-priority story that wanted one.
    with project.uow() as uow:
        best = max(linked, key=lambda a: a.quality_score)
        articles = {a.id: a for a in uow.articles.for_project(project.project_id)}
        priorities = [articles[a.article_id].priority for a in linked]
    assert articles[best.article_id].priority == max(priorities)


def test_templates_are_discovered_validated_and_duplicated(application):
    ids = application.templates.ids()
    assert "broadsheet_fa_standard" in ids
    assert not application.templates.validate("broadsheet_fa_standard")

    copy = application.templates.duplicate("broadsheet_fa_standard", "my_paper", "My Paper")
    assert copy.id == "my_paper"
    assert not application.templates.is_builtin("my_paper")
    assert application.templates.delete("my_paper")


def test_builtin_templates_cannot_be_overwritten(application):
    from app.core.errors import TemplateError

    spec = application.templates.get("broadsheet_fa_standard")
    with pytest.raises(TemplateError):
        application.templates.save(spec)


def test_diagnostics_run_without_a_network_or_adobe(application):
    report = application.diagnostics.run(deep=False)
    names = [check.name for check in report.checks]
    for expected in ("Python", "SQLite", "Storage", "Permissions", "Templates", "Fonts"):
        assert expected in names
    assert all(check.status in ("ok", "info", "warning", "error") for check in report.checks)


def test_a_story_can_be_snapshotted_and_restored(application, project):
    """Snapshot and restore are what make an edit or a delete reversible."""
    with project.uow() as uow:
        article = uow.articles.for_project(project.project_id)[0]
        article_id, original_title, original_body = article.id, article.title, article.body

    snapshot = application.content.snapshot_article(project, article_id)
    assert snapshot and snapshot["title"] == original_title

    application.content.update_article(project, article_id, title="تیتر تازه")
    with project.uow() as uow:
        assert uow.articles.get(article_id).title == "تیتر تازه"

    application.content.restore_article(project, snapshot)
    with project.uow() as uow:
        restored = uow.articles.get(article_id)
    assert restored.title == original_title
    assert restored.body == original_body


def test_a_deleted_story_is_restored_with_its_id(application, project):
    with project.uow() as uow:
        article_id = uow.articles.for_project(project.project_id)[1].id
    snapshot = application.content.snapshot_article(project, article_id)

    assert application.content.delete_article(project, article_id)
    with project.uow() as uow:
        assert uow.articles.get(article_id) is None

    restored_id = application.content.restore_article(project, snapshot)
    assert restored_id == article_id
    with project.uow() as uow:
        assert uow.articles.get(article_id).title == snapshot["title"]


def test_the_running_order_can_be_restored(application, project):
    before = application.content.snapshot_order(project)
    shuffled = list(reversed(before))
    application.content.reorder(project, shuffled)
    assert application.content.snapshot_order(project) == shuffled
    application.content.reorder(project, before)
    assert application.content.snapshot_order(project) == before


def test_picture_assignments_can_be_restored(application, project):
    before = application.assets.snapshot_assignments(project)
    assert before

    asset_id = next(iter(before))
    with project.uow() as uow:
        article_id = uow.articles.for_project(project.project_id)[0].id
    application.assets.assign(project, asset_id, article_id)
    assert application.assets.snapshot_assignments(project)[asset_id] == article_id

    application.assets.restore_assignments(project, before)
    assert application.assets.snapshot_assignments(project) == before
