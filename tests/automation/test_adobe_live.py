"""The Adobe test project of specification §45.

These tests drive a real InDesign installation end to end: connect, create a
document from the template, place text and images, build several pages, check
overflow and export a PDF. They are skipped automatically when no Adobe
installation is detected, so the suite still runs on a build machine.

Run them with:  pytest -m adobe
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adobe.detect import detect_indesign, detect_photoshop
from app.layout.engine import LayoutEngine

pytestmark = pytest.mark.adobe

indesign_required = pytest.mark.skipif(
    not detect_indesign().installed, reason="Adobe InDesign is not installed on this machine"
)
photoshop_required = pytest.mark.skipif(
    not detect_photoshop().installed, reason="Adobe Photoshop is not installed on this machine"
)


@pytest.fixture
def controller(tmp_path):
    from app.adobe.indesign.controller import InDesignController

    controller = InDesignController(tmp_path)
    yield controller
    try:
        controller.close_all()
    finally:
        controller.disconnect()


@indesign_required
def test_connection(controller):
    """Adobe Connection Test."""
    strategy = controller.connect()
    assert strategy in ("com", "script-file", "queue")
    info = controller.health()
    assert info["installed"] is True
    assert info.get("version")


@indesign_required
def test_template_document(controller, template):
    """Template Test: the document geometry comes from the template."""
    info = controller.create_document(template, page_count=2)
    assert info["pages"] == 2
    assert info["width_mm"] == pytest.approx(template.page_width_mm, abs=0.5)
    assert controller.apply_template_styles(template)


@indesign_required
def test_text_and_image_frames(controller, template, article_blocks, sample_images):
    """Text Test and Image Test."""
    for block in article_blocks[:1]:
        if block.image is not None:
            block.image.path = str(sample_images[0])
    engine = LayoutEngine(template, candidates_per_page=3)
    plan = engine.plan_edition(1, {1: article_blocks[:3]}, page_count=1)
    controller.create_document(template, page_count=1)
    controller.apply_template_styles(template)
    result = controller.build_page(plan.pages[0], template)
    assert result["elements"]
    assert not [item for item in result["elements"] if item.get("error")]
    report = controller.page_report(1)
    assert report["items"]


@indesign_required
def test_multi_page_document(controller, template, article_blocks, sample_images):
    """Multi-page Test."""
    engine = LayoutEngine(template, candidates_per_page=3)
    plan = engine.plan_edition(1, {1: article_blocks[:3], 2: article_blocks[3:]}, page_count=2)
    data = controller.build_document(plan, template)
    assert len(data["pages"]) == 2
    assert isinstance(data["overflow"], list)


@indesign_required
def test_pdf_export(controller, template, article_blocks, tmp_path):
    """PDF Export Test."""
    engine = LayoutEngine(template, candidates_per_page=3)
    plan = engine.plan_edition(1, {1: article_blocks[:2]}, page_count=1)
    controller.build_document(plan, template)
    target = tmp_path / "edition.pdf"
    controller.export_pdf(target, template.pdf_preset("print"))
    assert target.exists() and target.stat().st_size > 1000
    preview = tmp_path / "page1.png"
    controller.render_preview(1, preview, dpi=90)
    assert preview.exists()


@photoshop_required
def test_photoshop_processing(tmp_path, sample_images):
    """Photoshop processes an image through its own scripting API."""
    from app.adobe.photoshop.controller import PhotoshopController

    controller = PhotoshopController(tmp_path)
    try:
        result = controller.process_image(
            sample_images[0],
            tmp_path / "processed.jpg",
            aspect=16 / 9,
            width_px=1600,
            dpi=300,
        )
        assert result["engine"] == "photoshop"
        assert Path(result["path"]).exists()
    finally:
        controller.disconnect()
