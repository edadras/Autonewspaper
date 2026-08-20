"""Adobe detection, script generation and the automation strategy chain.

These run everywhere. The tests that need a real Adobe installation are in
``test_adobe_live.py`` and are skipped when none is present.
"""

from __future__ import annotations

import json
import re

import pytest

from app.adobe.bridge import AdobeBridge, ScriptResult, working_directory
from app.adobe.detect import detect_indesign, detect_photoshop
from app.adobe.jsx import (
    ScriptBuilder,
    build_document_script,
    build_page_script,
    build_photoshop_script,
    build_probe_script,
    js,
    library_text,
)
from app.automation.strategy import OperationRouter, Tier
from app.core.errors import AutomationError
from app.layout.engine import LayoutEngine


@pytest.fixture
def page(template, article_blocks):
    engine = LayoutEngine(template, candidates_per_page=4)
    plan = engine.plan_edition(
        1,
        {1: article_blocks},
        page_count=1,
        publication_name="روزنامه صبح",
        edition_date="1405/05/29",
    )
    return plan.pages[0]


# ------------------------------------------------------------- detection --


def test_detection_never_raises_and_reports_its_method():
    for app in (detect_indesign(), detect_photoshop()):
        assert app.kind in ("indesign", "photoshop")
        assert isinstance(app.installed, bool)
        if not app.installed:
            assert app.notes, "a missing installation must explain itself"


def test_a_wrong_configured_path_is_reported_not_trusted(tmp_path):
    app = detect_indesign(str(tmp_path / "nope" / "InDesign.exe"))
    assert app.detection_method != "configured"
    assert any("does not exist" in note for note in app.notes)


# --------------------------------------------------------------- library --


def test_runtime_library_is_present_and_self_contained():
    for host in ("indesign", "photoshop"):
        text = library_text(host)
        assert "api.emit = function" in text, "the result protocol must be defined"
        assert "api.fail = function" in text
        assert "JSON.stringify" in text, "ExtendScript has no native JSON"
    assert "AINS.ID = (function" in library_text("indesign")
    assert "AINS.PS = (function" in library_text("photoshop")


def test_js_serialisation_escapes_persian_as_ascii():
    rendered = js({"headline": "زلزله شدید"})
    assert all(ord(c) < 128 for c in rendered)
    assert json.loads(rendered)["headline"] == "زلزله شدید"


def test_generated_script_is_pure_ascii_and_wrapped_in_a_guard(page, template):
    script = build_page_script(page, template)
    text = script.render()
    assert all(ord(c) < 128 for c in text)
    assert "try {" in text and "AINS.fail(e" in text
    assert text.rstrip().endswith("}());")


def test_document_script_carries_every_page_and_style(page, template):
    script = build_document_script([page], template, page_count=1)
    text = script.render()
    assert "AINS.ID.createDocument(" in text
    assert "AINS.ID.applyTemplateStyles(" in text
    assert "AINS.ID.buildDocument(" in text
    plan = json.loads(re.search(r"var __plan = (\{.*?\});\n", text, re.DOTALL).group(1))
    assert len(plan["pages"]) == 1
    kinds = {element["kind"] for element in plan["pages"][0]["elements"]}
    assert "text" in kinds
    for element in plan["pages"][0]["elements"]:
        rect = element["rect"]
        assert rect["width"] > 0 and rect["height"] > 0


def test_page_geometry_survives_the_round_trip(page, template):
    script = build_page_script(page, template)
    payload = json.loads(re.search(r"var __page = (\{.*?\});\n", script.render(), re.DOTALL).group(1))
    by_name = {element["id"]: element for element in payload["elements"]}
    for element in page.elements:
        serialised = by_name[element.frame_name]
        assert serialised["rect"]["x"] == pytest.approx(element.rect.x, abs=0.01)
        assert serialised["rect"]["height"] == pytest.approx(element.rect.height, abs=0.01)


def test_indesign_bounds_are_in_the_documented_order():
    from app.models.schemas import Rect

    assert Rect(x=10, y=20, width=30, height=40).to_indesign_bounds() == [20, 10, 60, 40]


def test_photoshop_script_describes_the_whole_recipe(tmp_path):
    script = build_photoshop_script(
        {
            "source": str(tmp_path / "in.jpg"),
            "target": str(tmp_path / "out.jpg"),
            "aspect": 1.7778,
            "width_px": 1600,
            "dpi": 300,
            "remove_background": True,
        }
    )
    text = script.render()
    assert "AINS.PS.processImage(" in text
    assert "removeBackground" in library_text("photoshop")


def test_probe_script_asks_for_the_pdf_presets():
    text = build_probe_script("indesign").render()
    assert "pdfExportPresets" in text


def test_builder_rejects_an_unknown_host():
    with pytest.raises(ValueError):
        library_text("illustrator")


# ---------------------------------------------------------------- result --


@pytest.mark.parametrize(
    "payload,ok",
    [
        ('{"ok": true, "data": {"pages": 2}, "log": []}', True),
        ('{"ok": false, "error": {"message": "boom"}}', False),
        ('noise {"ok": true, "data": 1} trailing', True),
        ("", False),
        ("not json at all", False),
    ],
)
def test_script_results_are_parsed_defensively(payload, ok):
    result = ScriptResult.parse(payload, "com", 0.1)
    assert result.ok is ok
    if not ok:
        assert result.message()


def test_failed_result_raises_with_context():
    from app.core.errors import ScriptExecutionError

    result = ScriptResult.parse('{"ok": false, "error": {"message": "no document"}}', "com", 0.0)
    with pytest.raises(ScriptExecutionError) as info:
        result.raise_for_status("build_page")
    assert "no document" in str(info.value)


# --------------------------------------------------------------- bridge --


def test_bridge_lists_the_full_priority_chain(tmp_path):
    bridge = AdobeBridge(detect_indesign(), working_directory(tmp_path, "indesign"))
    chain = bridge.fallback_chain()
    assert chain[:3] == ["com", "script-file", "queue"]
    assert chain[-2:] == ["ui-automation", "input-automation"]


def test_bridge_refuses_to_connect_without_an_installation(tmp_path):
    from app.adobe.detect import AdobeApp
    from app.core.errors import AdobeConnectionError

    bridge = AdobeBridge(AdobeApp(kind="indesign"), working_directory(tmp_path, "indesign"))
    with pytest.raises(AdobeConnectionError):
        bridge.connect(launch=False)


def test_queue_strategy_round_trips_a_job_file(tmp_path):
    """The queue path is exercised by simulating the resident runner."""
    import threading
    import time

    from app.adobe.bridge import QueueStrategy
    from app.adobe.detect import AdobeApp

    app = AdobeApp(kind="indesign", installed=True, scripts_dir=tmp_path / "scripts")
    strategy = QueueStrategy(app, tmp_path / "work")
    assert strategy.install()

    def fake_runner() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for job in strategy.queue_dir.glob("*.job.jsx"):
                result = job.with_name(job.name.replace(".job.jsx", ".result.json"))
                result.write_text('{"ok": true, "data": {"pages": 3}, "log": ["built"]}', encoding="utf-8")
                job.unlink()
                return
            time.sleep(0.05)

    threading.Thread(target=fake_runner, daemon=True).start()
    script = ScriptBuilder("indesign", "probe").emit("1").build()
    result = strategy.execute(script, timeout=15)
    assert result.ok
    assert result.data == {"pages": 3}
    assert result.strategy == "queue"


# -------------------------------------------------------------- strategy --


def test_router_degrades_through_the_chain():
    router = OperationRouter("export_pdf")
    router.add(Tier.SCRIPTING_API, lambda: (_ for _ in ()).throw(RuntimeError("COM down")))
    router.add(Tier.FILE_BASED, lambda: (_ for _ in ()).throw(RuntimeError("queue down")))
    router.add(Tier.UI_AUTOMATION, lambda: "done")
    outcome = router.execute()
    assert outcome.value == "done"
    assert outcome.tier is Tier.UI_AUTOMATION
    assert outcome.degraded
    assert len(outcome.attempts) == 3


def test_router_skips_unavailable_mechanisms():
    router = OperationRouter("save")
    router.add(Tier.SCRIPTING_API, lambda: "api", can_run=lambda: False)
    router.add(Tier.FILE_BASED, lambda: "file")
    assert router.execute().value == "file"


def test_router_raises_when_everything_fails():
    router = OperationRouter("impossible")
    router.add(Tier.SCRIPTING_API, lambda: (_ for _ in ()).throw(RuntimeError("no")))
    with pytest.raises(AutomationError):
        router.execute()


def test_input_automation_is_denied_unless_enabled():
    from app.automation.input_automation import InputAutomation
    from app.core.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        InputAutomation(enabled=False).click(10, 10)


def test_ui_automation_reports_unavailability_cleanly():
    from app.automation.ui_automation import UIAutomation

    automation = UIAutomation("indesign")
    description = automation.describe()
    assert description["host"] == "indesign"
    assert isinstance(description["supported"], bool)


def test_vision_matcher_finds_a_known_control():
    from PIL import Image, ImageDraw

    from app.automation.vision_automation import match_template

    screen = Image.new("RGB", (900, 600), "#202020")
    draw = ImageDraw.Draw(screen)
    draw.rectangle([400, 250, 520, 290], fill="#3a7bd5")
    draw.text((420, 265), "Export", fill="white")
    match = match_template(screen, screen.crop((400, 250, 520, 290)), 0.8)
    assert match is not None
    assert abs(match.x - 400) <= 2 and abs(match.y - 250) <= 2
    assert match_template(screen, Image.new("RGB", (120, 40), "#ff0000"), 0.8) is None
