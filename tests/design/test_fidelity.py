"""Was the design produced the one that was asked for?

Not whether it is good - whether it is what was specified. A font that is not
installed, a colour outside the gamut, a size the application rounded and a
layer that failed silently all produce something plausible that is not what
was requested, and none of them announces itself.
"""

from __future__ import annotations

import pytest

from app.design import Box, DesignFidelity, DesignPlan, Layer, LayerKind, Severity, ShapeKind
from app.design.fidelity import color_distance, parse_color
from app.formats import FORMATS


def _plan(format_name: str = "A3") -> DesignPlan:
    return DesignPlan.for_format(FORMATS.resolve(format_name), language="fa")


def _headline(plan: DesignPlan, **overrides) -> Layer:
    defaults = dict(
        name="headline",
        kind=LayerKind.TEXT,
        box=plan.mm(20, 30, 250, 80),
        text="عنوان",
        font="IRANSans",
        font_style="Bold",
        size_pt=48.0,
        leading_pt=52.0,
        color="#c2410c",
        alignment="right",
        direction="rtl",
    )
    defaults.update(overrides)
    return plan.add(Layer(**defaults))


def _report(plan: DesignPlan, layer: Layer, **overrides) -> dict:
    entry = {
        "name": layer.name,
        "kind": "LayerKind.TEXT" if layer.is_text else "LayerKind.SOLIDFILL",
        "visible": True,
        "opacity": layer.opacity,
        "blend_mode": layer.blend_mode,
        "x": layer.box.x,
        "y": layer.box.y,
        "width": layer.box.width,
        "height": layer.box.height,
    }
    if layer.is_text:
        entry.update(
            {
                "text": layer.text,
                "size_pt": layer.size_pt,
                "leading_pt": layer.leading_pt,
                "tracking": layer.tracking,
                "alignment": layer.alignment,
                "font": f"{layer.font}-{layer.font_style}",
                "color": layer.color,
            }
        )
    entry.update(overrides)
    return {
        "width": plan.canvas.width_px,
        "height": plan.canvas.height_px,
        "resolution": plan.canvas.dpi,
        "mode": plan.canvas.mode,
        "font_substitutions": [],
        "layers": [entry],
    }


# --------------------------------------------------- a faithful result passes
def test_a_design_built_exactly_as_asked_reports_nothing():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer))

    assert result.faithful, [d.message for d in result.differences]
    assert result.checked == 1


# ---------------------------------------------------------------- the fonts
def test_a_substituted_font_is_blocking():
    plan = _plan()
    layer = _headline(plan)
    report = _report(plan, layer, font="MyriadPro-Regular")
    result = DesignFidelity(plan).check(report)

    font = next(d for d in result.differences if d.attribute == "font")
    assert font.severity is Severity.BLOCKING
    assert "IRANSans" in font.message and "MyriadPro" in font.message
    assert "Install" in font.suggestion


def test_a_font_the_host_could_not_find_at_all_is_reported():
    plan = _plan()
    layer = _headline(plan)
    report = _report(plan, layer)
    report["font_substitutions"] = [{"asked": "IRANSans", "got": None}]

    result = DesignFidelity(plan).check(report)
    assert any(d.severity is Severity.BLOCKING and d.attribute == "font" for d in result.differences)


def test_a_declared_fallback_is_not_reported_as_a_substitution():
    """Falling back to a face the design named is doing as it was told."""
    plan = _plan()
    layer = _headline(plan, fallback_fonts=["Vazirmatn", "Tahoma"])
    result = DesignFidelity(plan).check(_report(plan, layer, font="Vazirmatn-Regular"))

    assert not [d for d in result.differences if d.attribute == "font"]


# ---------------------------------------------------------------- the type
@pytest.mark.parametrize(
    ("attribute", "produced", "expected_severity"),
    [
        ("size_pt", 46.0, Severity.NOTABLE),
        ("leading_pt", 60.0, Severity.NOTABLE),
        ("tracking", -40, Severity.MINOR),
        ("alignment", "left", Severity.NOTABLE),
    ],
)
def test_type_that_drifts_is_named(attribute, produced, expected_severity):
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, **{attribute: produced}))

    found = next(d for d in result.differences if d.attribute == attribute)
    assert found.severity is expected_severity
    assert found.asked != found.produced


def test_a_size_within_rounding_is_not_reported():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, size_pt=48.1))
    assert not [d for d in result.differences if d.attribute == "size_pt"]


def test_words_that_changed_are_blocking():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, text="something else"))

    text = next(d for d in result.differences if d.attribute == "text")
    assert text.severity is Severity.BLOCKING


# -------------------------------------------------------------- the colours
def test_a_colour_that_shifted_is_reported_with_how_far():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, color="#8b2f08"))

    colour = next(d for d in result.differences if d.attribute == "color")
    assert "levels apart" in colour.message
    assert "gamut" in colour.suggestion


def test_a_colour_within_a_few_levels_is_the_same_colour():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, color="#c2410a"))
    assert not [d for d in result.differences if d.attribute == "color"]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("#000000", "#000000", 0), ("#ffffff", "#000000", 255), ("#fff", "#ffffff", 0), ("#abc", None, None)],
)
def test_colours_are_compared_channel_by_channel(left, right, expected):
    assert color_distance(left, right) == expected


def test_a_colour_that_cannot_be_read_is_not_a_difference():
    assert parse_color("teal") is None
    assert color_distance("teal", "#000000") is None


# ------------------------------------------------------------- the geometry
def test_a_layer_that_never_got_built_is_blocking():
    plan = _plan()
    _headline(plan)
    result = DesignFidelity(plan).check({"layers": []})

    missing = next(d for d in result.differences if d.attribute == "exists")
    assert missing.severity is Severity.BLOCKING


def test_a_hidden_layer_is_blocking():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, visible=False))
    assert any(d.attribute == "visible" and d.severity is Severity.BLOCKING for d in result.differences)


def test_a_shape_that_moved_is_reported():
    plan = _plan()
    layer = plan.add(
        Layer(name="panel", kind=LayerKind.SHAPE, box=plan.mm(10, 10, 100, 100), color="#ffffff")
    )
    result = DesignFidelity(plan).check(_report(plan, layer, x=layer.box.x + 40))

    moved = next(d for d in result.differences if d.attribute == "position")
    assert moved.severity is Severity.NOTABLE


def test_a_canvas_at_the_wrong_size_is_blocking():
    plan = _plan()
    layer = _headline(plan)
    report = _report(plan, layer)
    report["width"] = 1000
    result = DesignFidelity(plan).check(report)

    assert any(d.layer == "canvas" and d.severity is Severity.BLOCKING for d in result.differences)


def test_a_print_piece_delivered_in_rgb_is_reported():
    plan = _plan("A3")
    assert plan.canvas.mode == "cmyk"
    layer = _headline(plan)
    report = _report(plan, layer)
    report["mode"] = "rgb"

    result = DesignFidelity(plan).check(report)
    mode = next(d for d in result.differences if d.attribute == "mode")
    assert "press" in mode.suggestion


# -------------------------------------------------- checked before building
def test_millimetres_typed_where_pixels_were_meant_are_caught():
    """A 250 px headline on a 3508 px poster cannot hold one line of 48 pt."""
    plan = _plan("A3")
    plan.add(
        Layer(
            name="headline",
            kind=LayerKind.TEXT,
            box=Box(x=20, y=30, width=250, height=80),
            text="عنوان",
            size_pt=48,
        )
    )
    result = DesignFidelity(plan).check_plan()

    found = next(d for d in result.differences if d.attribute == "box")
    assert "millimetres" in found.message
    assert "plan.mm" in found.suggestion


def test_the_same_design_in_millimetres_is_clean():
    plan = _plan("A3")
    plan.add(
        Layer(
            name="headline",
            kind=LayerKind.TEXT,
            box=plan.mm(20, 30, 250, 80),
            text="عنوان",
            size_pt=48,
        )
    )
    assert DesignFidelity(plan).check_plan().faithful


def test_a_small_but_deliberate_badge_is_left_alone():
    plan = _plan("A3")
    plan.add(
        Layer(
            name="badge",
            kind=LayerKind.SHAPE,
            shape=ShapeKind.ELLIPSE,
            box=plan.mm(10, 10, 18, 18),
            color="#cc0000",
        )
    )
    assert DesignFidelity(plan).check_plan().faithful


def test_a_layer_off_the_canvas_is_blocking():
    plan = _plan("Instagram post")
    plan.add(Layer(name="stray", kind=LayerKind.SHAPE, box=Box(x=5000, y=5000, width=100, height=100)))
    result = DesignFidelity(plan).check_plan()

    assert any(d.severity is Severity.BLOCKING for d in result.differences)


def test_text_under_the_caption_bar_is_reported():
    plan = _plan("Instagram story")
    plan.add(
        Layer(
            name="cta",
            role="cta",
            kind=LayerKind.TEXT,
            box=Box(x=60, y=1750, width=960, height=120),
            text="بزن بریم",
            size_pt=40,
        )
    )
    result = DesignFidelity(plan).check_plan()

    safe = next(d for d in result.differences if d.attribute == "safe_area")
    assert "caption" in safe.suggestion


def test_a_box_with_no_area_is_blocking():
    plan = _plan()
    plan.add(Layer(name="empty", kind=LayerKind.SHAPE, box=Box(x=10, y=10, width=0, height=50)))
    result = DesignFidelity(plan).check_plan()
    assert result.blocking()


# ---------------------------------------------------------------- reporting
def test_the_report_says_what_it_checked_and_what_it_found():
    plan = _plan()
    layer = _headline(plan)
    result = DesignFidelity(plan).check(_report(plan, layer, size_pt=40.0, font="Other-Regular"))

    assert "instruction(s) checked" in result.summary()
    assert not result.faithful
    assert len(result.blocking()) == 1
    data = result.to_dict()
    assert data["host"] == "photoshop" and data["faithful"] is False
    assert all({"layer", "attribute", "asked", "produced"} <= set(d) for d in data["differences"])
