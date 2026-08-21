"""The tools the specialists work with."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PIL import Image

from app.agents.studio_tools import (
    Artefact,
    Blackboard,
    build_design_tools,
    build_page_tools,
    build_video_tools,
)
from app.agents.tools import PermissionPolicy
from app.design.plan import DesignPlan

# ----------------------------------------------------------------- board ---


def test_the_board_is_safe_to_share_between_specialists() -> None:
    """Three specialists publish at once; nothing is lost."""
    board = Blackboard()
    errors: list[Exception] = []

    def publish(author: str) -> None:
        try:
            for index in range(50):
                board.publish(Artefact(name=f"{author}_{index}", kind="file", author=author))
                board.note(author, f"step {index}")
        except Exception as exc:  # noqa: BLE001 - the test is what it raised
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(name,)) for name in ("ps", "id", "pr")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(board.artefacts()) == 150
    assert len(board.notes()) == 150


def test_asking_for_a_design_that_does_not_exist_says_which_do(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "poster", "format": "A4"})

    result = registry.invoke("inspect_design", {"design": "typo"})

    assert not result.ok
    assert "typo" in result.error


# ---------------------------------------------------------------- design ---


def test_a_design_is_started_at_the_size_that_was_asked_for(studio_context) -> None:
    registry = build_design_tools(studio_context)

    result = registry.invoke("start_design", {"name": "poster", "format": "A3"})

    assert result.ok
    assert result.data["width_px"] == 3508
    assert result.data["height_px"] == 4961
    assert result.data["mode"] == "cmyk", "a print format is set up for print"
    assert result.data["aspect"] == "0.707:1"


def test_a_screen_format_is_set_up_in_rgb(studio_context) -> None:
    registry = build_design_tools(studio_context)

    result = registry.invoke("start_design", {"name": "post", "format": "instagram post"})

    assert result.data["mode"] == "rgb"
    assert (result.data["width_px"], result.data["height_px"]) == (1080, 1080)


def test_the_front_of_the_normal_band_is_not_restacked(studio_context) -> None:
    """A painting order of zero is a real order, not an unset one."""
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A5"})
    registry.invoke(
        "add_shape",
        {"design": "d", "name": "ground", "shape": "rectangle", "x": 0, "y": 0,
         "width": 100, "height": 100, "units": "percent", "depth": "background"},
    )

    result = registry.invoke(
        "add_text",
        {"design": "d", "name": "title", "text": "hello", "x": 10, "y": 10,
         "width": 80, "height": 10, "units": "percent"},
    )

    assert result.data["z"] == 0, "the first normal layer takes the front of its own band"
    plan = studio_context.board.design("d")
    assert plan.layer("ground").z < plan.layer("title").z


def test_named_depths_never_cross(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A5"})
    orders = {}
    for depth in ("top", "front", "normal", "behind", "background"):
        for index in range(3):
            result = registry.invoke(
                "add_shape",
                {"design": "d", "name": f"{depth}{index}", "shape": "rectangle",
                 "x": 0, "y": 0, "width": 10, "height": 10, "units": "percent", "depth": depth},
            )
            orders.setdefault(depth, []).append(result.data["z"])

    assert max(orders["background"]) < min(orders["behind"])
    assert max(orders["behind"]) < min(orders["normal"])
    assert max(orders["normal"]) < min(orders["front"])
    assert max(orders["front"]) < min(orders["top"])


def test_a_layer_can_be_replaced_by_name(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A5"})
    for text in ("first", "second"):
        registry.invoke(
            "add_text",
            {"design": "d", "name": "title", "text": text, "x": 0, "y": 0,
             "width": 50, "height": 10, "units": "percent"},
        )

    plan = studio_context.board.design("d")
    assert len(plan.layers) == 1
    assert plan.layer("title").text == "second"


def test_a_transparent_ground_is_accepted_only_where_it_means_something(studio_context) -> None:
    registry = build_design_tools(studio_context)

    result = registry.invoke(
        "start_design", {"name": "card", "format": "1080x1920 px", "background": "#00000000"}
    )
    assert result.ok

    refused = registry.invoke(
        "add_shape",
        {"design": "card", "name": "bar", "shape": "rectangle", "x": 0, "y": 0,
         "width": 10, "height": 10, "color": "#00000000"},
    )
    assert not refused.ok, "a fill has no alpha channel to set"


def test_a_persian_design_sets_type_right_to_left_by_default(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A4"})

    result = registry.invoke(
        "add_text",
        {"design": "d", "name": "t", "text": "سرخط", "x": 0, "y": 0,
         "width": 50, "height": 10, "units": "percent"},
    )

    assert result.data["direction"] == "rtl"
    assert result.data["alignment"] == "right"


def test_type_with_no_size_given_gets_a_readable_one(studio_context) -> None:
    """A forgotten size must not become a zero-point headline."""
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A4"})

    result = registry.invoke(
        "add_text",
        {"design": "d", "name": "t", "text": "x", "x": 0, "y": 0,
         "width": 100, "height": 20, "units": "mm"},
    )

    assert result.data["size_pt"] >= 6.0


def test_furniture_is_drawn_and_placed(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A3"})

    result = registry.invoke(
        "add_furniture",
        {"design": "d", "name": "panel", "kind": "ruled_box",
         "x_mm": 20, "y_mm": 40, "width_mm": 80, "height_mm": 60},
    )

    assert result.ok
    drawn = Path(result.data["path"])
    assert drawn.exists() and drawn.stat().st_size > 0
    with Image.open(drawn) as image:
        assert image.size[0] > 0


def test_the_same_piece_of_furniture_is_only_drawn_once(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A3"})
    spec = {"design": "d", "kind": "tint_panel", "x_mm": 0, "y_mm": 0,
            "width_mm": 50, "height_mm": 50}

    first = registry.invoke("add_furniture", {**spec, "name": "a"})
    second = registry.invoke("add_furniture", {**spec, "name": "b", "y_mm": 60})

    assert first.data["path"] == second.data["path"]


def test_a_picture_can_be_named_by_artefact_rather_than_by_path(studio_context, photograph) -> None:
    """This is the hand-off: one specialist's output is another's input."""
    studio_context.board.publish(
        Artefact(name="cutout", kind="file", path=str(photograph), author="photoshop")
    )
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A4"})

    result = registry.invoke(
        "place_photo",
        {"design": "d", "name": "picture", "path": "cutout", "x": 0, "y": 0,
         "width": 100, "height": 60, "units": "percent"},
    )

    assert result.ok
    assert result.data["path"] == str(photograph)


def test_a_picture_that_is_neither_a_file_nor_an_artefact_is_refused(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A4"})

    result = registry.invoke(
        "place_photo",
        {"design": "d", "name": "p", "path": "nowhere.png", "x": 0, "y": 0,
         "width": 10, "height": 10},
    )

    assert not result.ok
    assert "nowhere.png" in result.error


def test_building_without_photoshop_still_produces_the_file(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A5", "background": "#123456"})
    registry.invoke(
        "add_text",
        {"design": "d", "name": "t", "text": "سلام", "x": 5, "y": 40,
         "width": 90, "height": 20, "units": "percent", "color": "#ffffff"},
    )

    result = registry.invoke("build_in_photoshop", {"design": "d"})

    assert result.ok
    assert result.data["engine"] == "builtin"
    assert "not reachable" in result.data["note"]
    assert Path(result.data["path"]).exists()


def test_the_saved_design_can_be_read_back(studio_context) -> None:
    registry = build_design_tools(studio_context)
    registry.invoke("start_design", {"name": "d", "format": "A4"})
    registry.invoke(
        "add_shape",
        {"design": "d", "name": "s", "shape": "ellipse", "x": 10, "y": 10,
         "width": 40, "height": 40, "units": "percent", "color": "#c2410c"},
    )

    result = registry.invoke("save_design", {"design": "d"})

    reloaded = DesignPlan.load(result.data["path"])
    assert reloaded.layer("s").color == "#c2410c"


# ----------------------------------------------------------------- video ---


def test_an_edit_is_started_at_the_platform_size(studio_context) -> None:
    registry = build_video_tools(studio_context)

    result = registry.invoke("start_edit", {"name": "reel", "format": "instagram reel"})

    assert (result.data["width"], result.data["height"]) == (1080, 1920)
    assert result.data["safe_area"], "a vertical social format keeps its safe area"


def test_a_transition_needs_a_clip_on_either_side(studio_context, tmp_path) -> None:
    registry = build_video_tools(studio_context)
    registry.invoke("start_edit", {"name": "e", "format": "1080p"})
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    registry.invoke("add_clip", {"edit": "e", "item": str(clip)})

    result = registry.invoke(
        "add_transition", {"edit": "e", "transition": "Cross Dissolve", "after_clip": 0}
    )

    assert not result.ok
    assert "either side" in result.error


def test_an_overlay_belongs_above_the_footage(studio_context, photograph) -> None:
    registry = build_video_tools(studio_context)
    registry.invoke("start_edit", {"name": "e", "format": "1080p"})

    refused = registry.invoke(
        "add_overlay", {"edit": "e", "path": str(photograph), "track": 0}
    )
    accepted = registry.invoke(
        "add_overlay", {"edit": "e", "path": str(photograph), "track": 1}
    )

    assert not refused.ok
    assert accepted.ok


def test_an_effect_cannot_be_hung_on_a_clip_that_is_not_there(studio_context) -> None:
    registry = build_video_tools(studio_context)
    registry.invoke("start_edit", {"name": "e", "format": "1080p"})

    result = registry.invoke(
        "apply_video_effect", {"edit": "e", "clip": 0, "effect": "Gaussian Blur"}
    )

    assert not result.ok
    assert "no clips" in result.error


def test_premiere_being_absent_is_explained_rather_than_hidden(studio_context, tmp_path) -> None:
    registry = build_video_tools(studio_context)
    registry.invoke("start_edit", {"name": "e", "format": "1080p"})
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    registry.invoke("add_clip", {"edit": "e", "item": str(clip)})

    result = registry.invoke("build_in_premiere", {"edit": "e"})

    assert result.ok
    assert result.data["built"] is False
    assert "extension panel" in result.data["reason"]
    assert Path(result.data["plan"]).exists(), "the edit survives to be built later"


# ----------------------------------------------------------------- pages ---


def test_a_document_takes_its_styles_from_the_template(studio_context) -> None:
    registry = build_page_tools(studio_context)

    result = registry.invoke(
        "start_document", {"name": "doc", "format": "381x476 mm", "pages": 2, "columns": 6}
    )

    assert result.data["page_width_mm"] == 381.0
    assert "headline" in result.data["styles"]
    assert result.data["column_width_mm"] > 0


def test_margins_that_leave_no_page_are_refused(studio_context) -> None:
    registry = build_page_tools(studio_context)

    result = registry.invoke(
        "start_document",
        {"name": "doc", "format": "A5", "margin_inside_mm": 80, "margin_outside_mm": 80},
    )

    assert not result.ok
    assert "leave almost nothing" in result.error


def test_frames_may_not_be_stacked_on_each_other(studio_context) -> None:
    registry = build_page_tools(studio_context)
    registry.invoke("start_document", {"name": "doc", "format": "A3"})
    registry.invoke(
        "add_text_frame",
        {"document": "doc", "page": 1, "name": "a", "text": "one",
         "x_mm": 20, "y_mm": 20, "width_mm": 100, "height_mm": 40},
    )

    result = registry.invoke(
        "add_text_frame",
        {"document": "doc", "page": 1, "name": "b", "text": "two",
         "x_mm": 40, "y_mm": 30, "width_mm": 60, "height_mm": 30},
    )

    assert not result.ok
    assert "overlap" in result.error


def test_furniture_may_sit_under_the_copy_it_decorates(studio_context) -> None:
    """A tint panel behind a sidebar is not a collision - it is the point."""
    registry = build_page_tools(studio_context)
    registry.invoke("start_document", {"name": "doc", "format": "A3"})
    registry.invoke(
        "add_page_furniture",
        {"document": "doc", "page": 1, "name": "panel", "kind": "tint_panel",
         "x_mm": 20, "y_mm": 20, "width_mm": 100, "height_mm": 100},
    )

    result = registry.invoke(
        "add_text_frame",
        {"document": "doc", "page": 1, "name": "aside", "text": "متن کادر",
         "x_mm": 26, "y_mm": 26, "width_mm": 88, "height_mm": 88, "style": "sidebar"},
    )

    assert result.ok
    plan = registry.invoke("inspect_document", {"document": "doc", "page": 1}).data
    frames = {frame["name"]: frame for frame in plan["pages"][0]["frames"]}
    assert frames["panel"]["role"] == "furniture:tint_panel"
    assert plan["pages"][0]["frames"][0]["name"] == "panel", "the panel paints first"


def test_a_picture_too_small_for_its_frame_is_reported(studio_context, tmp_path) -> None:
    small = tmp_path / "small.png"
    Image.new("RGB", (300, 200), (10, 10, 10)).save(small)
    registry = build_page_tools(studio_context)
    registry.invoke("start_document", {"name": "doc", "format": "A3"})

    result = registry.invoke(
        "add_picture_frame",
        {"document": "doc", "page": 1, "name": "p", "path": str(small),
         "x_mm": 20, "y_mm": 20, "width_mm": 200, "height_mm": 130},
    )

    assert result.ok, "it is placed - the operator decides, not the tool"
    assert result.data["effective_dpi"] < 60
    assert "dpi" in result.data["warning"]


def test_a_caption_becomes_its_own_frame(studio_context, photograph) -> None:
    registry = build_page_tools(studio_context)
    registry.invoke("start_document", {"name": "doc", "format": "A3"})

    result = registry.invoke(
        "add_picture_frame",
        {"document": "doc", "page": 1, "name": "photo", "path": str(photograph),
         "x_mm": 20, "y_mm": 20, "width_mm": 200, "height_mm": 120,
         "caption": "عکس: خبرگزاری"},
    )

    assert result.data["caption_frame"] == "photo_caption"
    document = registry.invoke("inspect_document", {"document": "doc"}).data
    names = [frame["name"] for frame in document["pages"][0]["frames"]]
    assert "photo_caption" in names


def test_a_page_exports_a_pdf_without_indesign(studio_context) -> None:
    registry = build_page_tools(studio_context)
    registry.invoke("start_document", {"name": "doc", "format": "A5"})
    registry.invoke(
        "add_text_frame",
        {"document": "doc", "page": 1, "name": "t", "text": "متن آزمایشی " * 20,
         "x_mm": 15, "y_mm": 15, "width_mm": 120, "height_mm": 150},
    )

    result = registry.invoke("export_document_pdf", {"document": "doc"})

    assert result.ok
    assert result.data["engine"] == "builtin"
    assert Path(result.data["path"]).stat().st_size > 1000


# ----------------------------------------------------------- permissions ---


@pytest.mark.parametrize(
    ("builder", "call"),
    [
        (build_design_tools, ("start_design", {"name": "d", "format": "A4"})),
        (build_video_tools, ("start_edit", {"name": "e", "format": "1080p"})),
        (build_page_tools, ("start_document", {"name": "doc", "format": "A4"})),
    ],
)
def test_a_withheld_capability_refuses_the_tool(studio_context, builder, call) -> None:
    """The arguments are valid; it is the permission that is missing."""
    name, arguments = call
    registry = builder(studio_context, PermissionPolicy(set()))

    result = registry.invoke(name, arguments)

    assert not result.ok
    assert "is not granted" in result.error


def test_a_specialist_only_holds_its_own_applications_tools(studio_context) -> None:
    """§55's permission check is also the division of labour."""
    from app.agents.studio import CAPABILITIES

    video = build_video_tools(studio_context, PermissionPolicy(CAPABILITIES["premiere"]))

    assert "start_edit" in video.names()
    refused = video.invoke("analyse_reference", {"path": "x"})
    assert not refused.ok  # studio.read is held, so this fails on the file, not the grant

    design = build_design_tools(studio_context, PermissionPolicy(CAPABILITIES["premiere"]))
    result = design.invoke("start_design", {"name": "d", "format": "A4"})
    assert not result.ok
    assert "is not granted" in result.error


# ------------------------------------------------------------- the story ---


def _threaded(studio_context, *, heights: tuple[float, ...] = (110.0, 110.0)):
    """A document with one long story and empty columns to run it into."""
    registry = build_page_tools(studio_context)
    registry.invoke("start_document", {"document": "d", "name": "d", "format": "A3", "columns": 6}
                    if False else {"name": "d", "format": "A3", "columns": 6})
    body = "متن طولانی برای ستون روزنامه. " * 120
    x = 16.0
    for index, height in enumerate(heights):
        registry.invoke(
            "add_text_frame",
            {"document": "d", "page": 1, "name": f"c{index + 1}", "text": body if index == 0 else "",
             "x_mm": x, "y_mm": 16, "width_mm": 80, "height_mm": height},
        )
        x += 84.0
    return registry


def test_threading_actually_flows_the_copy(studio_context) -> None:
    """A thread that only records a link leaves the second column blank."""
    registry = _threaded(studio_context)

    result = registry.invoke("thread_frames", {"document": "d", "from_frame": "c1", "to_frame": "c2"})

    assert result.ok, result.error
    words = {item["frame"]: item["words"] for item in result.data["frames"]}
    assert words["c1"] > 0 and words["c2"] > 0
    assert result.data["fits"], "the story fits the two columns together"
    document = registry.invoke("inspect_document", {"document": "d", "page": 1}).data
    frames = {frame["name"]: frame for frame in document["pages"][0]["frames"]}
    assert frames["c1"]["overflow"] == 0.0
    assert frames["c2"]["text"], "the continuation carries the rest of the story"
    assert frames["c1"]["text"] != frames["c2"]["text"]


def test_a_story_can_run_through_three_columns(studio_context) -> None:
    registry = _threaded(studio_context, heights=(60.0, 60.0, 60.0))
    registry.invoke("thread_frames", {"document": "d", "from_frame": "c1", "to_frame": "c2"})

    result = registry.invoke("thread_frames", {"document": "d", "from_frame": "c2", "to_frame": "c3"})

    assert result.data["story"] == ["c1", "c2", "c3"]
    assert all(item["words"] > 0 for item in result.data["frames"])


def test_a_story_may_not_run_in_a_circle(studio_context) -> None:
    registry = _threaded(studio_context, heights=(60.0, 60.0, 60.0))
    registry.invoke("thread_frames", {"document": "d", "from_frame": "c1", "to_frame": "c2"})
    registry.invoke("thread_frames", {"document": "d", "from_frame": "c2", "to_frame": "c3"})

    result = registry.invoke("thread_frames", {"document": "d", "from_frame": "c3", "to_frame": "c1"})

    assert not result.ok
    assert "circle" in result.error


def test_a_frame_that_already_has_its_own_copy_is_not_overwritten(studio_context) -> None:
    registry = _threaded(studio_context)
    registry.invoke(
        "add_text_frame",
        {"document": "d", "page": 1, "name": "other", "text": "متن دیگر",
         "x_mm": 184, "y_mm": 16, "width_mm": 80, "height_mm": 40},
    )

    result = registry.invoke("thread_frames", {"document": "d", "from_frame": "c1", "to_frame": "other"})

    assert not result.ok
    assert "copy of its own" in result.error


def test_removing_a_frame_unhooks_it_from_the_story(studio_context) -> None:
    registry = _threaded(studio_context)
    registry.invoke("thread_frames", {"document": "d", "from_frame": "c1", "to_frame": "c2"})

    result = registry.invoke("remove_frame", {"document": "d", "frame": "c2"})

    assert result.ok
    plan = studio_context._documents["d"].plan  # noqa: SLF001 - the test looks at the model
    head = plan.pages[0].elements[0]
    assert head.id == "c1"
    assert head.meta.get("threads_to") == []


# --------------------------------------------------------- the photograph --


def _photoshop_context(studio_context, tmp_path):
    """A context whose Photoshop controller is real but has no Photoshop."""
    from app.adobe.detect import detect_photoshop
    from app.adobe.photoshop.controller import PhotoshopController

    studio_context.adobe.photoshop = PhotoshopController(
        tmp_path / "psd-work", app=detect_photoshop()
    )
    return studio_context


def test_a_cut_out_that_cannot_be_found_is_reported_rather_than_faked(
    studio_context, tmp_path
) -> None:
    """A busy background has no edge to find; a mangled subject is worse than none."""
    import random

    busy = tmp_path / "busy.png"
    picture = Image.new("RGB", (400, 300))
    pixels = picture.load()
    random.seed(7)
    for y in range(300):
        for x in range(400):
            pixels[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    picture.save(busy)
    context = _photoshop_context(studio_context, tmp_path)
    registry = build_design_tools(context)

    result = registry.invoke("prepare_photo", {"path": str(busy), "cut_out": True})

    assert result.ok, "the picture is still prepared"
    assert any("Photoshop" in note for note in result.data["notes"])
    with Image.open(result.data["path"]) as image:
        assert image.mode == "RGB", "nothing was cut out, so nothing pretends to be"


def test_a_photograph_is_cut_out_and_cropped(studio_context, photograph, tmp_path) -> None:
    context = _photoshop_context(studio_context, tmp_path)
    registry = build_design_tools(context)

    result = registry.invoke(
        "prepare_photo",
        {"path": str(photograph), "name": "subject", "cut_out": True, "aspect": "4:5"},
    )

    assert result.ok, result.error
    produced = Path(result.data["path"])
    assert produced.exists()
    with Image.open(produced) as image:
        assert image.mode in ("RGBA", "LA"), "a cut-out keeps its transparency"
        assert abs(image.size[0] / image.size[1] - 0.8) < 0.02
    assert any("locally" in note for note in result.data["notes"]), (
        "it says the local engine did the work rather than passing it off as Photoshop"
    )


def test_the_prepared_photograph_is_published_for_the_crew(studio_context, photograph, tmp_path) -> None:
    context = _photoshop_context(studio_context, tmp_path)
    registry = build_design_tools(context)
    registry.invoke("prepare_photo", {"path": str(photograph), "name": "hero"})

    registry.invoke("start_design", {"name": "d", "format": "A4"})
    placed = registry.invoke(
        "place_photo",
        {"design": "d", "name": "p", "path": "hero", "x": 0, "y": 0,
         "width": 100, "height": 60, "units": "percent"},
    )

    assert placed.ok, "another specialist places it by name"
    assert "hero" in placed.data["path"]


@pytest.mark.parametrize("bad", ["16:0", "sideways", "-3"])
def test_an_aspect_that_is_not_one_is_refused(studio_context, photograph, tmp_path, bad) -> None:
    context = _photoshop_context(studio_context, tmp_path)
    registry = build_design_tools(context)

    result = registry.invoke("prepare_photo", {"path": str(photograph), "aspect": bad})

    assert not result.ok
    assert "aspect" in result.error
