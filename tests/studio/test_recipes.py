"""The recipes a specialist follows when no model is driving it."""

from __future__ import annotations

import pytest

from app.agents import recipes
from app.agents.autonomous import AgentStep
from app.agents.tools import ToolResult
from app.creative.style import StyleBrief, Swatch
from app.formats.registry import FormatRegistry


@pytest.fixture
def formats() -> FormatRegistry:
    return FormatRegistry()


def _play(recipe: recipes.Recipe, limit: int = 60) -> list[str]:
    """Run a recipe's planner to exhaustion, as the agent loop would."""
    planner = recipe.planner()
    steps: list[AgentStep] = []
    seen: list[str] = []
    for _ in range(limit):
        step = planner(steps)
        assert step is not None
        if step.done:
            return seen
        step.result = ToolResult(step.tool, True)
        steps.append(step)
        seen.append(step.tool)
    raise AssertionError("the recipe never finished")


def test_a_recipe_finishes_and_never_repeats_itself(formats) -> None:
    sheet = recipes.Sheet.resolve(formats, "A3")
    recipe = recipes.poster(
        design="p", format="A3", sheet=sheet, headline="سرخط", kicker="گزارش", detail="جزئیات"
    )

    played = _play(recipe)

    assert played[0] == "start_design"
    assert played[-1] == "save_design"
    assert len(played) == len(recipe.calls)


def test_a_recipe_resumes_where_the_model_left_off(formats) -> None:
    """The fallback picks up mid-loop, so it must not redo what was done."""
    sheet = recipes.Sheet.resolve(formats, "A4")
    recipe = recipes.poster(design="p", format="A4", sheet=sheet, headline="x")
    planner = recipe.planner()
    first = planner([])
    first.result = ToolResult(first.tool, True)

    second = planner([first])

    assert second.tool != first.tool


def test_a_call_that_failed_is_not_retried_forever(formats) -> None:
    sheet = recipes.Sheet.resolve(formats, "A4")
    recipe = recipes.poster(design="p", format="A4", sheet=sheet, headline="x")
    planner = recipe.planner()
    failed = planner([])
    failed.result = ToolResult(failed.tool, False, error="refused")

    following = planner([failed])

    assert following.tool != failed.tool


def test_the_poster_builds_the_ground_before_the_type(formats) -> None:
    sheet = recipes.Sheet.resolve(formats, "A3")
    recipe = recipes.poster(
        design="p", format="A3", sheet=sheet, headline="سرخط", photo="/tmp/x.jpg"
    )

    played = _play(recipe)

    assert played.index("place_photo") < played.index("add_text")
    assert played.index("add_furniture") < played.index("add_text")
    assert played.index("check_design") < played.index("build_in_photoshop")


def test_the_poster_wash_covers_the_lower_part_of_the_actual_sheet(formats) -> None:
    sheet = recipes.Sheet.resolve(formats, "A3")
    recipe = recipes.poster(
        design="p", format="A3", sheet=sheet, headline="x", photo="/tmp/x.jpg"
    )

    wash = next(call for name, call in recipe.calls if name == "add_furniture")

    assert wash["width_mm"] == pytest.approx(297.0, abs=0.5)
    assert wash["y_mm"] + wash["height_mm"] == pytest.approx(420.0, abs=0.5)


def test_a_poster_with_no_photograph_is_a_different_composition(formats) -> None:
    """An empty two thirds is a gap, not a design."""
    sheet = recipes.Sheet.resolve(formats, "A3")

    with_photo = recipes.poster(
        design="p", format="A3", sheet=sheet, headline="x", photo="/tmp/x.jpg"
    )
    without = recipes.poster(design="p", format="A3", sheet=sheet, headline="x")

    photo_headline = next(
        call for name, call in with_photo.calls if name == "add_text" and call["name"] == "headline"
    )
    type_headline = next(
        call for name, call in without.calls if name == "add_text" and call["name"] == "headline"
    )
    assert type_headline["y"] < photo_headline["y"], "the type moves up to fill the sheet"
    assert type_headline["height"] > photo_headline["height"]
    assert any(call["name"] == "field" for name, call in without.calls if name == "add_shape")
    assert not any(name == "add_furniture" for name, _ in without.calls), (
        "there is no picture to scrim"
    )


def test_the_title_card_is_built_on_a_transparent_ground(formats) -> None:
    recipe = recipes.title_card(design="t", format="instagram reel", headline="عنوان")

    start = next(call for name, call in recipe.calls if name == "start_design")
    build = next(call for name, call in recipe.calls if name == "build_in_photoshop")

    assert start["background"] == "#00000000"
    assert build["flatten"] is False, "flattening would fill the transparency"


def test_the_feature_image_sets_no_headline(formats) -> None:
    """The headline belongs on the page; setting it twice is a machine's mistake."""
    recipe = recipes.feature_image(
        design="f", width_mm=200, height_mm=120, photo="/tmp/x.jpg", caption="عکس: خبرگزاری"
    )

    texts = [call["text"] for name, call in recipe.calls if name == "add_text"]

    assert texts == ["عکس: خبرگزاری"]


def test_the_page_keeps_every_frame_inside_the_live_area(formats) -> None:
    sheet = recipes.Sheet.resolve(formats, "381x476 mm", columns=6, rtl=True)
    recipe = recipes.news_page(
        document="d",
        format="381x476 mm",
        sheet=sheet,
        headline="سرخط",
        body="متن " * 200,
        kicker="گزارش",
        sidebar="کادر " * 20,
        sidebar_heading="نکته‌ها",
    )

    frames = [
        call
        for name, call in recipe.calls
        if name in ("add_text_frame", "add_picture_frame", "add_page_furniture")
    ]
    assert frames
    for frame in frames:
        assert frame["x_mm"] >= sheet.live_x - 0.5, frame
        assert frame["y_mm"] >= sheet.live_y - 0.5, frame
        assert frame["x_mm"] + frame["width_mm"] <= sheet.live_x + sheet.live_width + 0.5, frame
        assert frame["y_mm"] + frame["height_mm"] <= sheet.live_y + sheet.live_height + 0.5, frame


def test_no_two_frames_of_a_page_are_stacked_on_each_other(formats) -> None:
    sheet = recipes.Sheet.resolve(formats, "A3", columns=6)
    recipe = recipes.news_page(
        document="d",
        format="A3",
        sheet=sheet,
        headline="سرخط",
        body="متن " * 200,
        kicker="گزارش",
        photo="/tmp/x.jpg",
        sidebar="کادر " * 20,
        sidebar_heading="نکته‌ها",
    )
    # The furniture is meant to sit under the copy, so it is not compared.
    frames = [
        call
        for name, call in recipe.calls
        if name in ("add_text_frame", "add_picture_frame")
        and not call["name"].startswith("sidebar")
    ]

    for index, first in enumerate(frames):
        for second in frames[index + 1 :]:
            across = min(first["x_mm"] + first["width_mm"], second["x_mm"] + second["width_mm"]) - max(
                first["x_mm"], second["x_mm"]
            )
            down = min(first["y_mm"] + first["height_mm"], second["y_mm"] + second["height_mm"]) - max(
                first["y_mm"], second["y_mm"]
            )
            assert across <= 0.6 or down <= 0.6, f"{first['name']} overlaps {second['name']}"


def test_a_persian_page_puts_its_sidebar_at_the_end_of_the_reading_direction(formats) -> None:
    sheet_rtl = recipes.Sheet.resolve(formats, "A3", rtl=True)
    sheet_ltr = recipes.Sheet.resolve(formats, "A3", rtl=False)
    common = {"format": "A3", "headline": "h", "body": "b" * 200, "sidebar": "s" * 40}

    rtl = recipes.news_page(document="d", sheet=sheet_rtl, **common)
    ltr = recipes.news_page(document="d", sheet=sheet_ltr, **common)

    panel_rtl = next(call for name, call in rtl.calls if name == "add_page_furniture")
    panel_ltr = next(call for name, call in ltr.calls if name == "add_page_furniture")
    assert panel_rtl["x_mm"] < panel_ltr["x_mm"]
    assert panel_rtl["x_mm"] == pytest.approx(sheet_rtl.live_x, abs=0.5)


def test_the_cut_puts_a_dissolve_on_every_join_and_no_more() -> None:
    recipe = recipes.social_cut(
        edit="e", format="1080p", footage=["a.mp4", "b.mp4", "c.mp4"], overlay="card"
    )

    clips = [call for name, call in recipe.calls if name == "add_clip"]
    transitions = [call for name, call in recipe.calls if name == "add_transition"]

    assert len(clips) == 3
    assert len(transitions) == 2
    assert [item["after_clip"] for item in transitions] == [0, 1]


def test_a_single_shot_gets_no_transition() -> None:
    recipe = recipes.social_cut(edit="e", format="1080p", footage=["a.mp4"])

    assert not [call for name, call in recipe.calls if name == "add_transition"]


def test_the_overlay_sits_above_the_footage() -> None:
    recipe = recipes.social_cut(edit="e", format="1080p", footage=["a.mp4"], overlay="card")

    overlay = next(call for name, call in recipe.calls if name == "add_overlay")

    assert overlay["track"] >= 1
    assert overlay["path"] == "card"


# --------------------------------------------------------------- palette ---


def test_a_palette_taken_from_a_reference_stays_readable() -> None:
    """A reference can easily suggest two colours nobody would set together."""
    brief = StyleBrief(background="#2b2b2b", foreground="#333333", accent="#2f2f2f")

    palette = recipes.Palette.from_brief(brief)

    from app.creative.style import contrast_ratio

    assert contrast_ratio(palette.ground, palette.ink) >= 4.5
    assert contrast_ratio(palette.ground, palette.accent) >= 2.0


def test_a_palette_with_no_reference_is_still_a_designed_one() -> None:
    palette = recipes.Palette.from_brief(None)

    from app.creative.style import contrast_ratio

    assert contrast_ratio(palette.ground, palette.ink) >= 7.0


def test_the_paper_colour_is_taken_from_the_reference_when_it_names_one() -> None:
    brief = StyleBrief(
        background="#101820",
        foreground="#ffffff",
        palette=[Swatch(hex="#efe9dd", role="paper", share=0.3)],
    )

    palette = recipes.Palette.from_brief(brief)

    assert palette.paper == "#efe9dd"
