"""The Premiere Pro host.

Premiere is not installed on the machines these run on, and unlike InDesign
and Photoshop it has no COM path that could be stubbed - it is reached only
through an extension panel. So what is checked here is everything that can be
checked without it: the plan, the ExtendScript that would run, the extension
that carries it, and the controller's honesty about not being able to
connect.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.formats import FORMATS
from app.video import EditPlan, SequenceSpec, Step, StepKind

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "app" / "adobe" / "cep" / "AINewspaperStudio"


@pytest.fixture
def reel_plan(tmp_path):
    """A short vertical edit with footage, a cut, an overlay and a fade."""
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    overlay = tmp_path / "title.png"
    for path in (clip_a, clip_b, overlay):
        path.write_bytes(b"stand-in")

    plan = EditPlan.for_format(FORMATS.resolve("reel"), name="Reel", language="fa")
    plan.add_clip(str(clip_a), at=0.0, in_point=0.0, out_point=4.0, note="opening")
    plan.add_clip(str(clip_b), at=4.0, in_point=2.0, out_point=8.0, note="the point")
    plan.add(
        Step(action=StepKind.TRANSITION, track=0, after_clip=0, transition="Cross Dissolve", duration=0.8)
    )
    plan.add(Step(action=StepKind.OVERLAY, track=1, at=0.5, duration=3.0, path=str(overlay), fade=0.4))
    plan.add(
        Step(action=StepKind.EFFECT, track=0, clip=0, effect="Lumetri Color", parameters={"Exposure": 0.2})
    )
    return plan


# ------------------------------------------------------------------ the plan
def test_a_sequence_takes_its_size_and_rate_from_the_format():
    spec = SequenceSpec.for_format(FORMATS.resolve("reel"))
    assert (spec.width, spec.height) == (1080, 1920)
    assert spec.fps == 30

    hd = SequenceSpec.for_format(FORMATS.resolve("1080p"))
    assert (hd.width, hd.height, hd.fps) == (1920, 1080, 25)


def test_a_vertical_edit_carries_the_platform_safe_area():
    plan = EditPlan.for_format(FORMATS.resolve("reel"))
    assert plan.safe_area is not None
    assert plan.safe_area["bottom"] > 0


def test_a_landscape_edit_has_the_broadcast_safe_area_only():
    plan = EditPlan.for_format(FORMATS.resolve("1080p"))
    assert plan.safe_area == {"top": 0.05, "bottom": 0.05, "left": 0.05, "right": 0.05}


def test_adding_a_clip_registers_its_footage(reel_plan):
    assert len(reel_plan.footage) == 2
    assert all(Path(path).exists() for path in reel_plan.footage)
    # The step refers to the item by name, which is what Premiere calls it.
    assert reel_plan.steps[0].item == "a.mp4"


def test_the_duration_comes_from_the_steps(reel_plan):
    # 0-4s then 4-10s: the second clip is trimmed 2s to 8s.
    assert reel_plan.duration == pytest.approx(10.0)


def test_a_plan_round_trips_through_a_file(reel_plan, tmp_path):
    again = EditPlan.load(reel_plan.save(tmp_path / "edit.json"))
    assert [step.action for step in again.steps] == [step.action for step in reel_plan.steps]
    assert again.sequence.fps == reel_plan.sequence.fps


# ------------------------------------------------- what Premiere is asked to do
def test_the_premiere_payload_carries_every_step(reel_plan):
    payload = reel_plan.to_premiere()
    assert payload["sequence"]["width"] == 1080
    actions = [step["action"] for step in payload["steps"]]
    assert actions == ["clip", "clip", "transition", "overlay", "effect"]
    assert payload["steps"][4]["parameters"] == {"Exposure": 0.2}


def test_empty_fields_are_left_out_of_the_payload():
    step = Step(action=StepKind.CLIP, item="a.mp4")
    payload = step.to_premiere()
    assert "transition" not in payload and "path" not in payload
    assert payload == {"action": "clip", "track": 0, "item": "a.mp4"}


def test_the_generated_script_is_valid_extendscript_and_pure_ascii(reel_plan):
    esprima = pytest.importorskip("esprima")
    from app.adobe.jsx import ScriptBuilder

    builder = ScriptBuilder("premiere", "build_edit")
    builder.call("setup")
    builder.var("__plan", reel_plan.to_premiere())
    builder.raw("var __result = AINS.PPRO.buildEdit(__plan);")
    builder.emit("__result")
    source = builder.build().render()

    esprima.parseScript(source)
    assert source.isascii()
    assert "AINS.PPRO.buildEdit" in source


def test_the_premiere_runtime_parses_and_covers_the_surface():
    esprima = pytest.importorskip("esprima")
    from app.adobe.jsx import library_text

    source = library_text("premiere")
    esprima.parseScript(source)
    for entry in (
        "createProject",
        "createSequence",
        "importFiles",
        "appendClip",
        "addTransition",
        "applyEffect",
        "transformClip",
        "addOverlay",
        "fadeClip",
        "addAudio",
        "exportSequence",
        "exportFrame",
        "buildEdit",
        "timelineReport",
    ):
        assert f"api.{entry} = function" in source, entry


def test_the_script_builder_uses_premieres_own_namespace():
    from app.adobe.jsx import ScriptBuilder

    builder = ScriptBuilder("premiere", "x")
    builder.call("setup", assign="__i")
    assert "AINS.PPRO.setup()" in builder.build().render()


# ------------------------------------------------------------- the extension
def test_the_extension_manifest_targets_premiere():
    manifest = ET.parse(EXTENSION / "CSXS" / "manifest.xml").getroot()
    hosts = [host.get("Name") for host in manifest.iter("Host")]
    assert hosts == ["PPRO"]
    assert manifest.get("ExtensionBundleId") == "com.ainewspaperstudio.premiere"


def test_the_extension_ships_everything_the_panel_loads():
    for required in ("index.html", "runner.js", "CSInterface.js", "jsx/bootstrap.jsx"):
        assert (EXTENSION / required).exists(), required


def test_the_panel_javascript_parses():
    esprima = pytest.importorskip("esprima")
    for name in ("runner.js", "CSInterface.js"):
        esprima.parseScript((EXTENSION / name).read_text(encoding="utf-8"))


def test_the_panel_claims_a_job_before_running_it():
    """Two panels, or a reopened one, must not run the same job twice."""
    source = (EXTENSION / "runner.js").read_text(encoding="utf-8")
    assert "deleteFile" in source, "a claimed job must be taken out of the queue"
    assert "STOP" in source, "the panel has to honour the stop file"


# ------------------------------------------------------------ the controller
def test_premiere_is_never_offered_a_com_path(tmp_path):
    """It registers no automation object, so trying COM only wastes a timeout."""
    from app.adobe.premiere.controller import PremiereController

    controller = PremiereController(tmp_path / "work")
    assert "com" not in controller.bridge.fallback_chain()
    assert controller.bridge.fallback_chain()[0] == "queue"


def test_connecting_without_premiere_says_so_plainly(tmp_path):
    from app.adobe.detect import AdobeApp
    from app.adobe.premiere.controller import PremiereController
    from app.core.errors import AdobeConnectionError

    controller = PremiereController(tmp_path / "work", app=AdobeApp(kind="premiere"))
    assert controller.available() is False
    with pytest.raises(AdobeConnectionError) as caught:
        controller.connect(launch=False)
    assert caught.value.recovery_action


def test_importing_footage_that_is_not_there_is_refused_before_premiere_is_asked(tmp_path):
    from app.adobe.premiere.controller import PremiereController
    from app.core.errors import AutomationError

    controller = PremiereController(tmp_path / "work")
    with pytest.raises(AutomationError, match="do not exist"):
        controller.import_files([tmp_path / "nothing.mp4"])


def test_the_controller_says_how_premiere_is_reached(tmp_path):
    from app.adobe.premiere.controller import PremiereController

    described = PremiereController(tmp_path / "work").describe()
    assert "extension" in described["note"].lower()


def test_premiere_is_detected_alongside_the_other_two():
    from app.adobe.detect import detect_all

    apps = detect_all()
    assert set(apps) == {"indesign", "photoshop", "premiere"}
    assert apps["premiere"].kind == "premiere"


def test_a_misspelt_instruction_is_refused_rather_than_ignored():
    """Silently dropping a field is how an edit ends up subtly wrong."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Step(action=StepKind.TRANSITION, transitionn="Cross Dissolve")


def test_a_transition_on_the_first_cut_still_says_which_cut():
    """Index zero is a real answer, not an empty one."""
    payload = Step(action=StepKind.TRANSITION, transition="Dip to Black", after_clip=0).to_premiere()
    assert payload["after_clip"] == 0
    assert payload["transition"] == "Dip to Black"


def test_an_effect_on_the_first_clip_still_says_which_clip():
    payload = Step(action=StepKind.EFFECT, effect="Gaussian Blur", clip=0).to_premiere()
    assert payload["clip"] == 0


def test_each_action_sends_only_the_fields_it_uses():
    overlay = Step(action=StepKind.OVERLAY, path="/x/title.png", at=1.0, fade=0.5, effect="ignored me")
    payload = overlay.to_premiere()
    assert "effect" not in payload, "an overlay has no effect field"
    assert payload["path"] == "/x/title.png" and payload["fade"] == 0.5
