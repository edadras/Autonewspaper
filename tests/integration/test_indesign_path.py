"""The InDesign branch of the pipeline, exercised without Adobe installed.

A recording double stands in for the controller so the orchestration around
InDesign - building the document, rendering previews from it, reading its page
report, re-applying corrected pages and exporting - is covered on any machine.
The double implements exactly the surface the pipeline and the exporter use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models.schemas import LayoutPlan
from app.vision.renderer import PreviewRenderer


class RecordingInDesign:
    """A stand-in that records calls and produces real files."""

    def __init__(self, template, previews_dir: Path, *, overflow: bool = False) -> None:
        self.template = template
        self.previews_dir = Path(previews_dir)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.overflow = overflow
        self._renderer = PreviewRenderer(template, dpi=80)
        self._plan: LayoutPlan | None = None
        self.built_into = ""
        self.built_page_mm: tuple[float, float] | None = None

    # -- session ---------------------------------------------------------
    def available(self) -> bool:
        return True

    def connect(self, launch: bool = True) -> str:
        self.calls.append(("connect", ()))
        return "com"

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))

    # -- the document already open ---------------------------------------
    #: Set to a geometry dictionary to stand for a document the operator has
    #: open; ``None`` means InDesign has nothing open.
    open_document: dict[str, Any] | None = None

    def describe_open_document(self) -> dict[str, Any] | None:
        self.calls.append(("describe_open_document", ()))
        return dict(self.open_document) if self.open_document else None

    def adopt_open_document(self) -> dict[str, Any] | None:
        self.calls.append(("adopt_open_document", ()))
        return dict(self.open_document) if self.open_document else None

    def ensure_pages(self, count: int) -> int:
        self.calls.append(("ensure_pages", (count,)))
        return count

    # -- building --------------------------------------------------------
    def build_document(
        self,
        plan: LayoutPlan,
        template,
        *,
        template_document: str | None = None,
        use_open_document: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("build_document", (len(plan.pages), template_document, use_open_document)))
        self._plan = plan
        self.built_into = (
            "open_document" if use_open_document else ("template_file" if template_document else "new")
        )
        self.built_page_mm = (template.page_width_mm, template.page_height_mm)
        return {"pages": [{"page": p.index} for p in plan.pages], "overflow": [], "strategy": "com"}

    def clear_page(self, page_index: int) -> int:
        self.calls.append(("clear_page", (page_index,)))
        return 1

    def build_page(self, page, template) -> dict[str, Any]:
        self.calls.append(("build_page", (page.index,)))
        return {"page": page.index, "elements": []}

    # -- inspection ------------------------------------------------------
    def render_preview(self, page_index: int, target, dpi: int = 110, fmt: str = "png") -> Path:
        self.calls.append(("render_preview", (page_index,)))
        page = self._page(page_index)
        target = Path(target)
        self._renderer.render_page(page, target.with_suffix(".png"))
        if target.suffix.lower() in (".jpg", ".jpeg"):
            from PIL import Image

            with Image.open(target.with_suffix(".png")) as image:
                image.convert("RGB").save(target, "JPEG", quality=85)
        return target

    def page_report(self, page_index: int) -> dict[str, Any]:
        self.calls.append(("page_report", (page_index,)))
        page = self._page(page_index)
        items = []
        for element in page.elements:
            entry: dict[str, Any] = {"id": element.frame_name}
            if element.is_text:
                entry["overflows"] = self.overflow and element.type.value == "body"
            items.append(entry)
        return {"page": page_index, "items": items}

    # -- output ----------------------------------------------------------
    def export_pdf(self, target, preset=None) -> Path:
        self.calls.append(("export_pdf", (getattr(preset, "id", None),)))
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4\n% recorded by the InDesign double\n")
        return target

    def export_idml(self, target) -> Path:
        self.calls.append(("export_idml", ()))
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"IDML")
        return target

    def save(self, target=None):
        self.calls.append(("save", ()))
        if target is None:
            return None
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"INDD")
        return target

    def package(self, folder) -> Path:
        self.calls.append(("package", ()))
        return Path(folder)

    def close_document(self, save: bool = False) -> bool:
        self.calls.append(("close_document", (save,)))
        return True

    # -- helpers ---------------------------------------------------------
    def _page(self, index: int):
        assert self._plan is not None, "build_document must run first"
        page = self._plan.page(index)
        assert page is not None
        return page

    def named(self, name: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [call for call in self.calls if call[0] == name]


@pytest.fixture
def with_indesign(application, project, template):
    """Point the application at the recording double."""
    double = RecordingInDesign(template, project.previews_dir)
    application.adobe.indesign_app.installed = True
    application.adobe.indesign_app.version = "2024"
    application.adobe._indesign = double  # noqa: SLF001 - test seam
    application.exporter.indesign = double
    return double


def test_the_document_is_built_and_exported_through_indesign(application, project, with_indesign):
    result = application.pipeline.run(project, mode="auto")

    assert result.success, result.errors
    assert result.adobe_strategy == "com"
    assert with_indesign.named("build_document"), "the document must be built in InDesign"
    assert with_indesign.named("export_pdf"), "the PDF must come from InDesign"
    assert result.indd_path and Path(result.indd_path).exists()
    assert result.idml_path and Path(result.idml_path).exists()
    assert not any("built-in renderer" in warning for warning in result.warnings)


def test_qa_reads_indesign_previews_and_its_page_report(application, project, with_indesign):
    application.pipeline.run(project, mode="auto")
    assert with_indesign.named("render_preview"), "QA must review what InDesign rendered"
    assert with_indesign.named("page_report"), "QA must use InDesign's own measurements"


def test_indesign_reported_overflow_becomes_a_qa_issue(application, project, template):
    """InDesign's own overflow report is authoritative and must reach QA."""
    double = RecordingInDesign(template, project.previews_dir, overflow=True)
    application.adobe.indesign_app.installed = True
    application.adobe._indesign = double  # noqa: SLF001 - test seam
    application.exporter.indesign = double

    application.pipeline.run(project, mode="auto")
    plan = LayoutPlan.load(project.layout_plan_path)
    content_pages = [page for page in plan.pages if page.elements and not page.meta.get("empty")]
    assert content_pages
    assert any(page.meta.get("qa", {}).get("issues", 0) > 0 for page in content_pages), (
        "the frames InDesign reported as overflowing must be flagged"
    )
    assert any("indesign" in page.meta.get("qa", {}).get("analyzed_by", []) for page in content_pages)


def test_corrected_pages_are_re_applied_to_the_document(application, project, with_indesign, monkeypatch):
    """A page the QA loop corrects must be rebuilt in InDesign, not left stale.

    The loop corrects a page in place, so the pipeline cannot detect a change
    by comparing the returned page with the original - it has to read the
    iteration log. This is the regression test for that.
    """
    from app.vision import qa_agent as qa_module

    original_run_loop = qa_module.VisionQAAgent.run_loop

    def run_loop_reporting_a_correction(self, page, render, **kwargs):
        result = original_run_loop(self, page, render, **kwargs)
        if result.iterations and page.elements:
            # Claim a correction was applied on the first iteration.
            result.iterations[0].correction = {
                "applied": [{"action": "shrink_element", "element_id": page.elements[-1].id}],
                "rejected": [],
                "improved": True,
            }
        return result

    monkeypatch.setattr(qa_module.VisionQAAgent, "run_loop", run_loop_reporting_a_correction)

    application.pipeline.run(project, mode="auto")

    rebuilt = with_indesign.named("build_page")
    cleared = with_indesign.named("clear_page")
    assert rebuilt, "corrected pages must be rebuilt in the document"
    assert len(cleared) == len(rebuilt), "a page must be emptied before it is rebuilt"
    for _name, args in rebuilt:
        assert any(call[1] == args for call in cleared)


def test_an_uncorrected_run_does_not_touch_the_pages_again(application, project, with_indesign):
    """Without corrections the document is built once and left alone."""
    application.pipeline.run(project, mode="auto")
    assert with_indesign.named("build_document")
    assert not with_indesign.named("clear_page")


def test_a_failing_indesign_falls_back_and_says_so(application, project, template, monkeypatch):
    double = RecordingInDesign(template, project.previews_dir)

    def broken(*_args, **_kwargs):
        raise RuntimeError("InDesign stopped responding")

    monkeypatch.setattr(double, "build_document", broken)
    application.adobe.indesign_app.installed = True
    application.adobe._indesign = double  # noqa: SLF001 - test seam
    application.exporter.indesign = double

    result = application.pipeline.run(project, mode="auto")
    assert result.success, result.errors
    assert result.adobe_strategy == "builtin-renderer"
    assert any("could not build" in warning.lower() for warning in result.warnings)
    assert result.pdf_paths and Path(result.pdf_paths[0]).exists()


def test_the_document_is_closed_when_the_run_finishes(application, project, with_indesign):
    application.pipeline.run(project, mode="auto")
    assert with_indesign.named("close_document")


# ------------------------------------------- the document already open in InDesign
def _open_geometry(**overrides) -> dict:
    """A document standing open in InDesign, described the way the JSX does."""
    geometry = {
        "name": "Front page.indd",
        "path": "C:/Work/Front page.indd",
        "modified": False,
        "pages": 1,
        "width_mm": 297.0,
        "height_mm": 420.0,
        "facing_pages": True,
        "bleed_mm": 3.0,
        "columns": 6,
        "gutter_mm": 4.0,
        "margins": {"top": 14.0, "bottom": 14.0, "inside": 14.0, "outside": 12.0},
        "version": "2024",
    }
    geometry.update(overrides)
    return geometry


def test_an_open_document_is_used_instead_of_a_new_one(application, project, with_indesign):
    """What the operator already has open is where the edition goes."""
    with_indesign.open_document = _open_geometry()

    result = application.pipeline.run(project, mode="auto")

    assert result.success, result.errors
    assert with_indesign.named("describe_open_document"), "InDesign was never asked what is open"
    assert with_indesign.built_into == "open_document"


def test_the_edition_is_planned_to_fit_the_open_document(application, project, with_indesign):
    """A document set up by hand rarely matches the template to the millimetre."""
    with_indesign.open_document = _open_geometry(
        name="Tabloid.indd",
        width_mm=280.0,
        height_mm=400.0,
        columns=5,
        gutter_mm=5.0,
        margins={"top": 16.0, "bottom": 18.0, "inside": 16.0, "outside": 13.0},
    )

    result = application.pipeline.run(project, mode="auto")

    assert result.success, result.errors
    assert with_indesign.built_into == "open_document"
    assert with_indesign.built_page_mm == (280.0, 400.0)

    plan = LayoutPlan.load(project.layout_plan_path)
    assert (plan.pages[0].width_mm, plan.pages[0].height_mm) == (280.0, 400.0)
    assert plan.pages[0].columns == 5
    # Every frame has to sit on the page it is actually being placed on.
    for page in plan.pages:
        for element in page.elements:
            assert element.rect.right <= page.width_mm + 0.5, element.id
            assert element.rect.bottom <= page.height_mm + 0.5, element.id
    assert any("Tabloid.indd" in warning for warning in result.warnings)


def test_a_document_the_operator_opened_is_left_open(application, project, with_indesign):
    """Closing it - discarding their changes - is not ours to do."""
    application.settings.set_path("adobe.close_documents_on_finish", True)
    with_indesign.open_document = _open_geometry()

    application.pipeline.run(project, mode="auto")

    assert not with_indesign.named("close_document")


def test_a_document_we_created_is_still_closed(application, project, with_indesign):
    application.settings.set_path("adobe.close_documents_on_finish", True)
    with_indesign.open_document = None

    application.pipeline.run(project, mode="auto")

    assert with_indesign.built_into == "new"
    assert with_indesign.named("close_document")


def test_the_open_document_can_be_ignored_by_setting(application, project, with_indesign):
    application.settings.set_path("adobe.document_source", "new_document")
    with_indesign.open_document = _open_geometry(width_mm=280.0, height_mm=400.0)

    result = application.pipeline.run(project, mode="auto")

    assert result.success
    assert with_indesign.built_into == "new"
    assert not with_indesign.named("describe_open_document")
    assert with_indesign.built_page_mm == (297.0, 420.0)


def test_a_mismatched_page_is_refused_when_adapting_is_off(application, project, with_indesign):
    application.settings.set_path("adobe.adopt_open_geometry", False)
    with_indesign.open_document = _open_geometry(name="A4.indd", width_mm=210.0, height_mm=297.0)

    result = application.pipeline.run(project, mode="auto")

    assert with_indesign.built_into == "new"
    assert any("A4.indd" in warning and "new document" in warning for warning in result.warnings)


def test_asking_for_an_open_document_when_there_is_none_says_so(application, project, with_indesign):
    application.settings.set_path("adobe.document_source", "open_document")
    with_indesign.open_document = None

    result = application.pipeline.run(project, mode="auto")

    assert result.success
    assert with_indesign.built_into == "new"
    assert any("no document" in warning.lower() for warning in result.warnings)


def test_the_templates_own_indesign_file_is_opened_when_it_names_one(
    application, project, with_indesign, tmp_path
):
    document = tmp_path / "house-style.indt"
    document.write_bytes(b"not really an indd, but it exists")
    with_indesign.open_document = None
    application.templates.get_or_default(project.project()["template_id"]).indesign_template_path = str(
        document
    )

    result = application.pipeline.run(project, mode="auto")

    assert result.success
    assert with_indesign.built_into == "template_file"


def test_a_missing_template_document_falls_back_with_a_warning(application, project, with_indesign):
    with_indesign.open_document = None
    application.templates.get_or_default(
        project.project()["template_id"]
    ).indesign_template_path = "C:/gone/missing.indt"

    result = application.pipeline.run(project, mode="auto")

    assert with_indesign.built_into == "new"
    assert any("missing.indt" in warning for warning in result.warnings)
