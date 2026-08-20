"""Designs: the plan, what Photoshop is asked to build, and the proof render.

Photoshop is not installed on the machines these run on, so the plan and the
generated ExtendScript are checked directly, and everything visual is checked
against the built-in renderer - which is also what the operator sees before
anything is built for real.
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.design import Box, DesignPlan, Effects, Layer, LayerKind, ShapeKind
from app.design.renderer import DesignRenderer, _rgba
from app.formats import FORMATS


@pytest.fixture
def story_plan():
    """A small but complete social design."""
    plan = DesignPlan.for_format(FORMATS.resolve("Instagram story"), name="Test", language="fa")
    plan.add(
        Layer(
            name="panel",
            kind=LayerKind.SHAPE,
            shape=ShapeKind.ROUNDED,
            radius=40,
            box=Box(x=60, y=400, width=960, height=800),
            color="#ffffff",
        )
    )
    plan.add(
        Layer(
            name="headline",
            kind=LayerKind.TEXT,
            role="headline",
            box=Box(x=120, y=520, width=840, height=300),
            text="اقتصاد ایران در سالی که گذشت",
            size_pt=72,
            leading_pt=90,
            color="#0b1d3a",
            direction="rtl",
            alignment="right",
        )
    )
    return plan


# ------------------------------------------------------------------ the plan
def test_a_plan_takes_its_canvas_from_the_format():
    plan = DesignPlan.for_format(FORMATS.resolve("Instagram story"))
    assert (plan.canvas.width_px, plan.canvas.height_px) == (1080, 1920)
    assert plan.canvas.medium == "social"


def test_a_print_format_is_planned_in_cmyk_and_a_screen_one_in_rgb():
    assert DesignPlan.for_format(FORMATS.resolve("A3")).canvas.mode == "cmyk"
    assert DesignPlan.for_format(FORMATS.resolve("Instagram post")).canvas.mode == "rgb"


def test_a_platform_safe_area_reaches_the_plan():
    plan = DesignPlan.for_format(FORMATS.resolve("Instagram story"))
    assert plan.canvas.safe_box is not None
    assert plan.canvas.box.contains(plan.canvas.safe_box)


def test_a_print_format_carries_its_bleed_in_pixels():
    plan = DesignPlan.for_format(FORMATS.resolve("A3"))
    assert plan.canvas.bleed_px == pytest.approx(3 / 25.4 * 300, abs=1)


def test_layers_are_built_back_to_front(story_plan):
    story_plan.add(Layer(name="top", kind=LayerKind.SHAPE, box=Box(width=10, height=10)))
    order = [layer.name for layer in story_plan.ordered()]
    assert order == ["panel", "headline", "top"]


def test_a_plan_round_trips_through_a_file(story_plan, tmp_path):
    path = story_plan.save(tmp_path / "design.json")
    again = DesignPlan.load(path)
    assert [layer.name for layer in again.layers] == [layer.name for layer in story_plan.layers]
    assert again.layers[1].text == story_plan.layers[1].text


# ----------------------------------------------------- what Photoshop is sent
def test_the_photoshop_payload_carries_every_layer(story_plan):
    payload = story_plan.to_photoshop()
    assert payload["canvas"]["width_px"] == 1080
    kinds = [layer["kind"] for layer in payload["layers"]]
    assert kinds == ["shape", "text"]
    headline = payload["layers"][1]
    assert headline["direction"] == "rtl"
    assert headline["size_pt"] == 72
    assert headline["leading_pt"] == 90


def test_leading_defaults_to_something_sensible_rather_than_zero():
    layer = Layer(name="t", kind=LayerKind.TEXT, text="x", size_pt=40)
    assert layer.to_photoshop()["leading_pt"] == pytest.approx(48)


def test_effects_are_only_sent_when_there_are_any():
    plain = Layer(name="a", kind=LayerKind.SHAPE)
    assert "effects" not in plain.to_photoshop()
    styled = Layer(name="b", kind=LayerKind.SHAPE, effects=Effects(shadow={"size": 20}))
    assert styled.to_photoshop()["effects"] == {"shadow": {"size": 20}}


def test_the_generated_script_is_valid_extendscript_and_pure_ascii(story_plan, tmp_path):
    esprima = pytest.importorskip("esprima")
    from app.adobe.jsx import build_design_script

    script = build_design_script(
        story_plan.to_photoshop(), export={"path": str(tmp_path / "out.png"), "quality": 92}
    )
    source = script.render()

    esprima.parseScript(source)
    assert source.isascii(), "Persian text must be escaped, not embedded"
    assert "AINS.PSD.buildDesign" in source
    assert "AINS.PSD.exportTo" in source


def test_the_photoshop_runtime_parses_as_extendscript():
    esprima = pytest.importorskip("esprima")
    from app.adobe.jsx import library_text

    source = library_text("photoshop")
    esprima.parseScript(source)
    for entry in ("createCanvas", "createText", "createShape", "placeFile", "applyEffects", "buildDesign"):
        assert f"api.{entry} = function" in source, entry


# --------------------------------------------------------------- the render
def test_a_design_renders_at_the_canvas_size(story_plan, tmp_path):
    path = DesignRenderer(story_plan).render_to(tmp_path / "design.png")
    with Image.open(path) as image:
        assert image.size == (1080, 1920)


def test_a_preview_scales_the_whole_design(story_plan, tmp_path):
    path = DesignRenderer(story_plan, scale=0.25).render_to(tmp_path / "small.png")
    with Image.open(path) as image:
        assert image.size == (270, 480)


def test_the_text_is_measured_from_the_glyphs_not_the_plan(story_plan):
    measured = {item["name"]: item for item in DesignRenderer(story_plan).measure()}
    headline = measured["headline"]
    assert headline["lines"] >= 1
    assert headline["measured_width"] > 0
    assert headline["overflow"] == 0.0


def test_text_that_cannot_fit_reports_an_overflow():
    plan = DesignPlan.for_format(FORMATS.resolve("Instagram post"))
    plan.add(
        Layer(
            name="cramped",
            kind=LayerKind.TEXT,
            box=Box(x=40, y=40, width=300, height=60),
            text="یک متن بسیار طولانی که به هیچ وجه در این کادر کوچک جا نمی‌شود و باید سرریز گزارش شود",
            size_pt=40,
            direction="rtl",
        )
    )
    measured = DesignRenderer(plan).measure()[0]
    assert measured["overflow"] > 0


def test_a_missing_picture_is_drawn_as_a_marked_frame_not_a_crash(tmp_path):
    plan = DesignPlan.for_format(FORMATS.resolve("Instagram post"))
    plan.add(
        Layer(
            name="photo",
            kind=LayerKind.IMAGE,
            box=Box(x=100, y=100, width=400, height=400),
            path=str(tmp_path / "does-not-exist.jpg"),
        )
    )
    path = DesignRenderer(plan, scale=0.3).render_to(tmp_path / "out.png")
    assert path.exists()


def test_a_picture_is_placed_and_fitted(tmp_path):
    source = tmp_path / "photo.png"
    Image.new("RGB", (800, 400), "#ff0000").save(source)
    plan = DesignPlan.for_format(FORMATS.resolve("Instagram post"), background="#ffffff")
    plan.add(
        Layer(
            name="photo",
            kind=LayerKind.IMAGE,
            box=Box(x=0, y=0, width=1080, height=1080),
            path=str(source),
            fit="cover",
        )
    )
    with Image.open(DesignRenderer(plan).render_to(tmp_path / "out.png")) as image:
        # "cover" fills the square, so the middle is the photograph's red.
        assert image.getpixel((540, 540))[:3] == (255, 0, 0)


def test_a_gradient_is_fast_enough_for_a_full_frame(tmp_path):
    """Evaluated per pixel this took thirteen seconds for one layer."""
    import time

    plan = DesignPlan.for_format(FORMATS.resolve("Instagram story"))
    plan.add(
        Layer(
            name="sky",
            kind=LayerKind.SHAPE,
            box=Box(x=0, y=0, width=1080, height=1920),
            color="#ffffff",
            effects=Effects(gradient={"from": "#ff5f6d", "to": "#1f2a63", "angle": 115}),
        )
    )
    started = time.monotonic()
    image = DesignRenderer(plan).render()
    elapsed = time.monotonic() - started
    image.close()
    assert elapsed < 3.0, f"a single gradient layer took {elapsed:.1f}s"


def test_a_gradient_actually_changes_across_the_frame(tmp_path):
    plan = DesignPlan.for_format(FORMATS.custom(400, 400, "px"))
    plan.add(
        Layer(
            name="ramp",
            kind=LayerKind.SHAPE,
            box=Box(x=0, y=0, width=400, height=400),
            color="#ffffff",
            effects=Effects(gradient={"from": "#000000", "to": "#ffffff", "angle": 0}),
        )
    )
    image = DesignRenderer(plan).render()
    left = image.getpixel((10, 200))[0]
    right = image.getpixel((390, 200))[0]
    image.close()
    assert abs(right - left) > 100, "the ramp is flat"


def test_opacity_and_clipping_are_honoured(tmp_path):
    plan = DesignPlan.for_format(FORMATS.custom(200, 200, "px"), background="#000000")
    plan.add(
        Layer(name="mask", kind=LayerKind.SHAPE, box=Box(x=0, y=0, width=100, height=200), color="#ffffff")
    )
    plan.add(
        Layer(
            name="paint",
            kind=LayerKind.SHAPE,
            box=Box(x=0, y=0, width=200, height=200),
            color="#ff0000",
            clip_to_below=True,
        )
    )
    image = DesignRenderer(plan).render()
    inside = image.getpixel((50, 100))[:3]
    outside = image.getpixel((150, 100))[:3]
    image.close()
    assert inside == (255, 0, 0), "the clipped layer should paint over the mask"
    assert outside == (0, 0, 0), "and nowhere else"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#fff", (255, 255, 255, 255)),
        ("#000000", (0, 0, 0, 255)),
        ("#ff5f6d", (255, 95, 109, 255)),
        ("not a colour", (0, 0, 0, 255)),
    ],
)
def test_colours_are_read_forgivingly(value, expected):
    assert _rgba(value) == expected
