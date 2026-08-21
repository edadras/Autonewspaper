"""The decorated boxes a designer makes by hand.

A newspaper page is not only type in columns: it has tinted panels, ruled
boxes, section tabs, folio badges, arcs bleeding off the trim, broken rules
between teasers and a caption bar over a photograph. On a real desk somebody
draws those in Photoshop and places them in InDesign.
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.design import Box, DesignPlan, Effects, Layer, LayerKind
from app.design.furniture import (
    BLEED_PX,
    Furniture,
    FurnitureDesigner,
    FurnitureFactory,
    FurnitureSpec,
)
from app.design.renderer import DesignRenderer
from app.formats import FORMATS


def _spec(kind: Furniture, **overrides) -> FurnitureSpec:
    defaults = dict(kind=kind, width_mm=60, height_mm=30, dpi=120, accent="#1f4e9c", ink="#111111")
    defaults.update(overrides)
    return FurnitureSpec(**defaults)


# ------------------------------------------------------------- every piece
@pytest.mark.parametrize("kind", list(Furniture))
def test_every_piece_can_be_drawn(kind, tmp_path):
    factory = FurnitureFactory(tmp_path)
    path = factory.make(_spec(kind))

    assert path.exists()
    with Image.open(path) as image:
        assert image.mode == "RGBA", "furniture has to keep its transparency"
        assert image.size[0] > 0 and image.size[1] > 0


@pytest.mark.parametrize("kind", list(Furniture))
def test_no_piece_paints_a_black_ground(kind, tmp_path):
    """Flattening a transparent canvas puts a black box on the page."""
    path = FurnitureFactory(tmp_path).make(_spec(kind))
    with Image.open(path) as image:
        corner = image.convert("RGBA").getpixel((1, 1))
    assert corner[3] == 0, f"{kind.value} has an opaque corner"


def test_a_piece_is_drawn_once_per_edition(tmp_path):
    factory = FurnitureFactory(tmp_path)
    first = factory.make(_spec(Furniture.RULED_BOX))
    stamp = first.stat().st_mtime_ns
    second = factory.make(_spec(Furniture.RULED_BOX))

    assert first == second
    assert second.stat().st_mtime_ns == stamp, "it was drawn again"


def test_two_pieces_that_differ_get_different_files(tmp_path):
    factory = FurnitureFactory(tmp_path)
    blue = factory.make(_spec(Furniture.TINT_PANEL, accent="#1f4e9c"))
    gold = factory.make(_spec(Furniture.TINT_PANEL, accent="#e0aa2e"))
    assert blue != gold


def test_a_piece_is_drawn_larger_than_its_frame_and_placed_back_out(tmp_path):
    """An edge effect clipped at the frame is why panels look a size small."""
    spec = _spec(Furniture.SHADOW_CARD)
    factory = FurnitureFactory(tmp_path)
    with Image.open(factory.make(spec)) as image:
        assert image.width == spec.width_px + BLEED_PX * 2

    frame = Box(x=20, y=30, width=60, height=40)
    placed = factory.placement(spec, frame, spec.dpi)
    assert placed.x < frame.x and placed.right > frame.right
    assert placed.width > frame.width


# --------------------------------------------------------- the right shapes
def test_a_section_tab_has_square_shoulders_and_a_round_foot(tmp_path):
    path = FurnitureFactory(tmp_path).make(
        _spec(Furniture.SECTION_TAB, width_mm=26, height_mm=16, corner_mm=4)
    )
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        top_left = rgba.getpixel((BLEED_PX + 2, BLEED_PX + 2))
        bottom_left = rgba.getpixel((BLEED_PX + 2, rgba.height - BLEED_PX - 2))
    assert top_left[3] > 200, "the shoulders are square"
    assert bottom_left[3] < 100, "the foot is rounded away"


def test_a_page_badge_is_a_disc_unless_a_radius_is_asked_for(tmp_path):
    factory = FurnitureFactory(tmp_path)
    with Image.open(factory.make(_spec(Furniture.PAGE_BADGE, width_mm=16, height_mm=16))) as image:
        rgba = image.convert("RGBA")
        assert rgba.getpixel((BLEED_PX + 2, BLEED_PX + 2))[3] < 60, "a disc has empty corners"
        assert rgba.getpixel((rgba.width // 2, rgba.height // 2))[3] > 200


def test_a_dotted_rule_is_actually_broken(tmp_path):
    path = FurnitureFactory(tmp_path).make(
        _spec(Furniture.DOTTED_RULE, width_mm=60, height_mm=1, rule_pt=1.0)
    )
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        row = rgba.height // 2
        alphas = [rgba.getpixel((x, row))[3] for x in range(BLEED_PX, rgba.width - BLEED_PX)]
    assert max(alphas) > 200, "there is ink"
    assert min(alphas) < 40, "and gaps between it"


def test_a_flag_marker_is_a_triangle_that_follows_the_reading_direction(tmp_path):
    factory = FurnitureFactory(tmp_path)
    rtl = factory.make(_spec(Furniture.FLAG_MARKER, width_mm=10, height_mm=10, direction="rtl"))
    ltr = factory.make(_spec(Furniture.FLAG_MARKER, width_mm=10, height_mm=10, direction="ltr"))

    def corner(path, right: bool):
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
            x = rgba.width - BLEED_PX - 3 if right else BLEED_PX + 3
            return rgba.getpixel((x, rgba.height - BLEED_PX - 3))[3]

    assert corner(rtl, right=True) > 180, "right-to-left points from the right"
    assert corner(ltr, right=False) > 180, "left-to-right points from the left"


def test_an_edge_arc_is_half_a_disc(tmp_path):
    """It bleeds off one edge, so it is solid across the middle and curves away."""
    path = FurnitureFactory(tmp_path).make(_spec(Furniture.EDGE_ARC, width_mm=10, height_mm=20))
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        middle = rgba.height // 2
        near_edge = rgba.getpixel((BLEED_PX + 2, middle))[3]
        far_corner = rgba.getpixel((rgba.width - BLEED_PX - 2, BLEED_PX + 3))[3]

        def covered(row: int) -> int:
            return sum(1 for x in range(BLEED_PX, rgba.width - BLEED_PX) if rgba.getpixel((x, row))[3] > 128)

        at_middle = covered(middle)
        near_top = covered(BLEED_PX + 4)

    assert near_edge > 180, "solid where it meets the trim"
    assert far_corner < 80, "and curved away at the far corner"
    assert at_middle > near_top * 2, "it is widest across the middle, like a half-disc"


# ----------------------------------------------------------- the caption bar
def test_a_caption_bar_fades_in_rather_than_ending_in_a_hard_edge(tmp_path):
    """A hard-edged block over a photograph looks pasted on."""
    spec = _spec(Furniture.CAPTION_BAR, width_mm=70, height_mm=16)
    path = FurnitureFactory(tmp_path).make(spec)
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        column = rgba.width // 2
        top = rgba.getpixel((column, BLEED_PX + 2))[3]
        middle = rgba.getpixel((column, rgba.height // 2))[3]
        bottom = rgba.getpixel((column, rgba.height - BLEED_PX - 2))[3]

    assert top < 30, f"the top has to be nearly clear, not {top}"
    assert bottom > 150, "and the foot solid"
    assert top < middle < bottom, "with a real grade in between"


# ------------------------------------------------------------- the gradients
@pytest.mark.parametrize(
    ("angle", "expect"),
    [
        (0, "left_to_right"),
        (90, "bottom_to_top"),
        (180, "right_to_left"),
        (270, "top_to_bottom"),
    ],
)
def test_the_gradient_angle_means_what_photoshop_means_by_it(angle, expect):
    """The preview and Photoshop have to agree or the plan comes out mirrored."""
    plan = DesignPlan.for_format(FORMATS.custom(200, 200, "px"), background="#ffffff")
    plan.add(
        Layer(
            name="ramp",
            kind=LayerKind.SHAPE,
            box=Box(x=0, y=0, width=200, height=200),
            color="#ffffff",
            effects=Effects(gradient={"from": "#000000", "to": "#ffffff", "angle": angle}),
        )
    )
    image = DesignRenderer(plan).render()
    top, bottom = image.getpixel((100, 8))[0], image.getpixel((100, 192))[0]
    left, right = image.getpixel((8, 100))[0], image.getpixel((192, 100))[0]
    image.close()

    if expect == "left_to_right":
        assert left < right - 100
    elif expect == "right_to_left":
        assert right < left - 100
    elif expect == "bottom_to_top":
        assert bottom < top - 100
    else:
        assert top < bottom - 100


def test_a_gradient_completes_over_its_own_layer_not_the_whole_canvas():
    """A layer smaller than the canvas saw only the middle slice of the ramp."""
    plan = DesignPlan.for_format(FORMATS.custom(400, 400, "px"), background="#ffffff")
    plan.add(
        Layer(
            name="strip",
            kind=LayerKind.SHAPE,
            box=Box(x=0, y=300, width=400, height=60),
            color="#ffffff",
            effects=Effects(gradient={"from": "#000000", "to": "#ffffff", "angle": 270}),
        )
    )
    image = DesignRenderer(plan).render()
    top = image.getpixel((200, 303))[0]
    bottom = image.getpixel((200, 357))[0]
    image.close()

    assert top < 60, "the strip's own top must be the start of the ramp"
    assert bottom > 190, "and its own foot the end"


def test_a_gradients_own_transparency_survives_the_shape_mask():
    """Pasting through the shape alone throws the per-stop opacity away."""
    plan = DesignPlan.for_format(FORMATS.custom(200, 200, "px"), background="#ffffff")
    plan.add(
        Layer(
            name="fade",
            kind=LayerKind.SHAPE,
            box=Box(x=0, y=0, width=200, height=200),
            color="#000000",
            effects=Effects(
                gradient={
                    "stops": [
                        {"color": "#000000", "location": 0, "opacity": 0},
                        {"color": "#000000", "location": 100, "opacity": 100},
                    ],
                    "angle": 270,
                }
            ),
        )
    )
    image = DesignRenderer(plan).render()
    top, bottom = image.getpixel((100, 5))[0], image.getpixel((100, 195))[0]
    image.close()

    assert top > 200, "the clear end should let the white ground through"
    assert bottom < 60, "and the solid end should not"


# ---------------------------------------------------------------- the plan
def test_furniture_is_an_ordinary_design_plan():
    """So it goes through the same builder, renderer and fidelity check."""
    plan = FurnitureDesigner().plan(_spec(Furniture.RULED_BOX))
    assert isinstance(plan, DesignPlan)
    assert plan.layers
    assert plan.meta["furniture"] == "ruled_box"
    assert plan.canvas.background.endswith("00"), "the ground is transparent"


def test_photoshop_is_used_when_it_is_there_and_not_missed_when_it_is_not(tmp_path):
    class FakePhotoshop:
        def __init__(self):
            self.calls = 0

        def build_design(self, plan, export=None, flatten=True):
            self.calls += 1
            Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(export)
            return {"layers": [], "failed": []}

    photoshop = FakePhotoshop()
    path = FurnitureFactory(tmp_path, photoshop=photoshop).make(_spec(Furniture.TINT_PANEL))
    assert photoshop.calls == 1 and path.exists()


def test_a_photoshop_failure_still_leaves_the_page_its_furniture(tmp_path):
    class BrokenPhotoshop:
        def build_design(self, plan, export=None, flatten=True):
            raise RuntimeError("Photoshop went away")

    path = FurnitureFactory(tmp_path, photoshop=BrokenPhotoshop()).make(_spec(Furniture.TINT_PANEL))
    assert path.exists(), "the built-in renderer has to cover for it"


def test_a_transparent_design_refuses_to_be_written_as_jpeg(tmp_path):
    plan = FurnitureDesigner().plan(_spec(Furniture.RULED_BOX))
    with pytest.raises(ValueError, match="cannot be written as JPEG"):
        DesignRenderer(plan).render_to(tmp_path / "box.jpg")


# ------------------------------------------------- furniture on a real page
def test_furniture_is_placed_where_the_page_calls_for_it(template, article_blocks, tmp_path):
    from app.design.furnisher import FurnishingStyle, PageFurnisher
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {1: article_blocks}, page_count=1, sections={1: "تلویزیون"}).pages[0]
    before = len(page.elements)

    factory = FurnitureFactory(tmp_path)
    result = PageFurnisher(factory, style=FurnishingStyle(), dpi=150).furnish(page, direction="rtl")

    assert result.added, "a page with a section and a folio has furniture"
    assert len(page.elements) == before + len(result.added)
    kinds = {item.kind for item in result.added}
    assert Furniture.SECTION_TAB in kinds
    assert Furniture.PAGE_BADGE in kinds
    assert Furniture.FOOTER_RULE in kinds


def test_every_piece_sits_behind_the_type_and_is_locked(template, article_blocks, tmp_path):
    from app.design.furnisher import BACKGROUND_Z, PageFurnisher
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {1: article_blocks}, page_count=1, sections={1: "خبر"}).pages[0]
    PageFurnisher(FurnitureFactory(tmp_path), dpi=150).furnish(page)

    pieces = [e for e in page.elements if e.meta.get("furniture")]
    assert pieces
    for piece in pieces:
        assert piece.z_index <= BACKGROUND_Z, f"{piece.id} would cover the type"
        assert piece.locked, "the operator did not place it, so it is not theirs to drag"
        assert piece.meta["decorative"] is True, "QA must not measure it as a photograph"
        assert piece.fit_mode == "none", "it is drawn to size; cropping shaves the edge effect"


def test_each_piece_records_why_it_is_there(template, article_blocks, tmp_path):
    """Furniture that decorates nothing is what makes a page look designed-at."""
    from app.design.furnisher import PageFurnisher
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {1: article_blocks}, page_count=1, sections={1: "خبر"}).pages[0]
    result = PageFurnisher(FurnitureFactory(tmp_path), dpi=150).furnish(page)

    for item in result.added:
        assert item.reason, f"{item.kind.value} was placed for no stated reason"


def test_turning_a_piece_off_leaves_it_off(template, article_blocks, tmp_path):
    from app.design.furnisher import FurnishingStyle, PageFurnisher
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {1: article_blocks}, page_count=1, sections={1: "خبر"}).pages[0]
    style = FurnishingStyle(section_tab=False, edge_arcs=False, folio_badge=False)
    result = PageFurnisher(FurnitureFactory(tmp_path), style=style, dpi=150).furnish(page)

    kinds = {item.kind for item in result.added}
    assert Furniture.SECTION_TAB not in kinds
    assert Furniture.EDGE_ARC not in kinds
    assert Furniture.PAGE_BADGE not in kinds


def test_a_transparent_piece_does_not_paint_black_on_the_page(template, tmp_path):
    """The page renderer threw the alpha away and every piece had a black box."""
    from app.layout.engine import LayoutEngine
    from app.models.schemas import ElementSpec, ElementType, Rect
    from app.vision.renderer import PreviewRenderer

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {}, page_count=1).pages[0]
    piece = FurnitureFactory(tmp_path).make(_spec(Furniture.PAGE_BADGE, width_mm=20, height_mm=20, dpi=150))
    page.elements.append(
        ElementSpec(
            id="badge",
            type=ElementType.IMAGE,
            rect=Rect(x=40, y=40, width=30, height=30),
            z_index=-50,
            image_path=str(piece),
            fit_mode="none",
        )
    )

    rendered = PreviewRenderer(template, dpi=70).render_page(page, tmp_path / "page.png")
    with Image.open(rendered.path) as image:
        rgb = image.convert("RGB")
        # The corner of the badge's frame is transparent, so the paper shows.
        corner = rgb.getpixel((int(41 / page.width_mm * rgb.width), int(41 / page.height_mm * rgb.height)))
    assert min(corner) > 180, f"the paper should show through, not {corner}"


def test_a_photograph_is_still_cropped_to_fill_its_box(template, tmp_path):
    """Honouring the fit mode must not stop a picture box behaving like one."""
    from app.layout.engine import LayoutEngine
    from app.models.schemas import ElementSpec, ElementType, Rect
    from app.vision.renderer import PreviewRenderer

    wide = tmp_path / "wide.png"
    Image.new("RGB", (900, 200), "#cc2200").save(wide)
    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {}, page_count=1).pages[0]
    page.elements.append(
        ElementSpec(
            id="photo",
            type=ElementType.IMAGE,
            rect=Rect(x=40, y=60, width=60, height=60),
            image_path=str(wide),
            fit_mode="fill",
        )
    )

    rendered = PreviewRenderer(template, dpi=70).render_page(page, tmp_path / "page.png")
    with Image.open(rendered.path) as image:
        rgb = image.convert("RGB")
        middle = rgb.getpixel((int(70 / page.width_mm * rgb.width), int(90 / page.height_mm * rgb.height)))
    assert middle[0] > 150 and middle[1] < 90, "the square box should be filled by the photograph"
