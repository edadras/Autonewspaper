"""Type reversed out of a band, and haloes round a cut-out."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.design.plan import Box, Canvas, DesignPlan, Layer, LayerKind, ShapeKind
from app.design.renderer import DesignRenderer

STRIPE = (240, 180, 40)
BAND = (16, 24, 40)


@pytest.fixture
def striped(tmp_path: Path) -> Path:
    """A picture nobody could mistake for a flat colour."""
    target = tmp_path / "stripes.png"
    picture = Image.new("RGB", (800, 500))
    draw = ImageDraw.Draw(picture)
    for x in range(0, 800, 40):
        draw.rectangle([x, 0, x + 20, 500], fill=STRIPE)
    picture.save(target)
    return target


def _reversed_design(striped: Path, *, knockout: bool = True) -> DesignPlan:
    plan = DesignPlan(
        name="reversed",
        canvas=Canvas(width_px=800, height_px=500, dpi=150, background="#ffffff"),
    )
    plan.add(
        Layer(
            name="photo", kind=LayerKind.IMAGE, box=Box(x=0, y=0, width=800, height=500),
            path=str(striped), z=0,
        )
    )
    plan.add(
        Layer(
            name="band", kind=LayerKind.SHAPE, box=Box(x=0, y=170, width=800, height=160),
            shape=ShapeKind.RECTANGLE, color="#101828", z=10,
        )
    )
    plan.add(
        Layer(
            name="head", kind=LayerKind.TEXT, box=Box(x=40, y=190, width=720, height=120),
            text="REVERSED", size_pt=64, color="#ffffff", alignment="center",
            knockout=knockout, z=20,
        )
    )
    return plan


def test_a_layer_given_an_order_of_zero_keeps_it() -> None:
    """Zero is a real painting order; treating it as unset restacks the layer."""
    plan = DesignPlan(name="d", canvas=Canvas(width_px=100, height_px=100, dpi=72))
    plan.add(Layer(name="ground", kind=LayerKind.SHAPE, box=Box(width=100, height=100), z=0))
    plan.add(Layer(name="over", kind=LayerKind.SHAPE, box=Box(width=50, height=50), z=5))

    assert [layer.name for layer in plan.ordered()] == ["ground", "over"]


def test_a_layer_with_no_order_given_goes_on_top() -> None:
    plan = DesignPlan(name="d", canvas=Canvas(width_px=100, height_px=100, dpi=72))
    plan.add(Layer(name="first", kind=LayerKind.SHAPE, box=Box(width=100, height=100), z=40))
    plan.add(Layer(name="second", kind=LayerKind.SHAPE, box=Box(width=50, height=50)))

    assert plan.layer("second").z > 40


def test_the_letters_become_holes_in_the_band(striped: Path) -> None:
    plan = _reversed_design(striped)

    image = DesignRenderer(plan).render().convert("RGB")

    # Inside a letter the picture shows; beside it the band does.
    middle = image.height // 2
    row = [image.getpixel((x, middle)) for x in range(60, 740)]
    assert any(pixel == STRIPE for pixel in row), "no picture shows through the type"
    assert any(abs(pixel[0] - BAND[0]) < 12 for pixel in row), "the band is gone entirely"
    image.close()


def test_without_the_knockout_the_type_is_drawn_over_the_band(striped: Path) -> None:
    """The comparison that proves the knockout did something."""
    reversed_out = DesignRenderer(_reversed_design(striped)).render().convert("RGB")
    printed = DesignRenderer(_reversed_design(striped, knockout=False)).render().convert("RGB")

    assert reversed_out.tobytes() != printed.tobytes()
    middle = reversed_out.height // 2
    through = sum(
        1 for x in range(60, 740) if reversed_out.getpixel((x, middle)) == STRIPE
    )
    over = sum(1 for x in range(60, 740) if printed.getpixel((x, middle)) == STRIPE)
    assert through > over, "the picture shows through the reversed type and not the printed one"
    reversed_out.close()
    printed.close()


def test_the_band_outside_the_letters_is_untouched(striped: Path) -> None:
    plan = _reversed_design(striped)

    image = DesignRenderer(plan).render().convert("RGB")

    # A point inside the band but well above the type.
    assert image.getpixel((400, 178))[:3] == BAND
    image.close()


def test_a_knockout_with_nothing_beneath_it_is_simply_drawn() -> None:
    """It cannot cut a hole in the canvas, and must not vanish either."""
    plan = DesignPlan(
        name="alone", canvas=Canvas(width_px=300, height_px=200, dpi=72, background="#ffffff")
    )
    plan.add(
        Layer(
            name="head", kind=LayerKind.TEXT, box=Box(x=10, y=60, width=280, height=80),
            text="ALONE", size_pt=40, color="#000000", knockout=True, z=0,
        )
    )

    image = DesignRenderer(plan).render().convert("RGB")

    assert any(pixel != (255, 255, 255) for pixel in image.getdata()), "the type disappeared"
    image.close()


def test_two_layers_can_be_reversed_out_of_the_same_band(striped: Path) -> None:
    """A kicker and a headline both cut into the band, not into each other."""
    plan = _reversed_design(striped)
    kicker = Layer(
        name="kicker", kind=LayerKind.TEXT, box=Box(x=40, y=176, width=720, height=20),
        text="KICKER", size_pt=14, color="#ffffff", alignment="center",
        knockout=True, z=30,
    )
    plan.add(kicker)
    renderer = DesignRenderer(plan)

    # Where the kicker's own ink lands, rather than where a font might put it.
    drawn = renderer._draw_layer(kicker, (800, 500))  # noqa: SLF001 - the test measures
    bounds = drawn.getchannel("A").getbbox()
    drawn.close()
    assert bounds, "the kicker drew nothing at all"

    image = renderer.render().convert("RGB")
    middle = (bounds[1] + bounds[3]) // 2
    row = [image.getpixel((x, middle)) for x in range(bounds[0], bounds[2])]
    image.close()

    assert any(pixel == STRIPE for pixel in row), "the second knockout did nothing"


def test_a_halo_round_a_cut_out_grows_beyond_it(tmp_path: Path) -> None:
    """A stroke on a cut-out subject is what a halo actually is."""
    subject = tmp_path / "subject.png"
    cut = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(cut).ellipse([60, 60, 140, 140], fill=(30, 30, 30, 255))
    cut.save(subject)

    plan = DesignPlan(
        name="halo", canvas=Canvas(width_px=200, height_px=200, dpi=72, background="#ffffff")
    )
    layer = Layer(
        name="subject", kind=LayerKind.IMAGE, box=Box(x=0, y=0, width=200, height=200),
        path=str(subject), fit="contain", z=0,
    )
    layer.effects.stroke = {"color": "#c2410c", "size": 8, "opacity": 100}
    plan.add(layer)

    image = DesignRenderer(plan).render().convert("RGB")

    # Just outside the disc there is now accent colour where the paper was.
    ring = image.getpixel((100, 55))
    image.close()
    assert ring[0] > ring[2] + 40, f"no halo: {ring}"


def test_the_hole_goes_in_the_layer_it_was_told_to_cut(striped: Path) -> None:
    """A kicker between the headline and its band must not take the hole.

    Pairing a knockout with whatever happens to precede it in the stack is
    right only while nothing comes between the type and its band. As soon as
    something does - and on a real cover something always does - the headline
    cuts a hole in the kicker instead and disappears.
    """
    plan = _reversed_design(striped, knockout=False)
    plan.add(
        Layer(
            name="kicker", kind=LayerKind.TEXT, box=Box(x=40, y=176, width=720, height=18),
            text="KICKER", size_pt=12, color="#ffffff", alignment="center", z=15,
        )
    )
    head = plan.layer("head")
    head.knockout = True
    head.knockout_of = "band"
    head.z = 20  # above the kicker, so the kicker is the layer just beneath

    renderer = DesignRenderer(plan)
    drawn = renderer._draw_layer(head, (800, 500))  # noqa: SLF001 - the test measures
    bounds = drawn.getchannel("A").getbbox()
    drawn.close()
    assert bounds

    image = renderer.render().convert("RGB")
    middle = (bounds[1] + bounds[3]) // 2
    row = [image.getpixel((x, middle)) for x in range(bounds[0], bounds[2])]
    image.close()

    assert any(pixel == STRIPE for pixel in row), (
        "the headline cut into the kicker instead of the band, so it vanished"
    )


def test_a_knockout_naming_a_layer_that_is_not_there_falls_back(striped: Path) -> None:
    """Better the type drawn plainly than the type gone."""
    plan = _reversed_design(striped)
    plan.layer("head").knockout_of = "no_such_layer"

    image = DesignRenderer(plan).render().convert("RGB")

    middle = image.height // 2
    row = [image.getpixel((x, middle)) for x in range(60, 740)]
    image.close()
    assert any(pixel == STRIPE for pixel in row), "the headline disappeared entirely"
