"""Every size the studio can be asked for.

The catalogue is the foundation the poster, cover, social and video work
stands on, so the sizes are checked against their published values rather
than against themselves.
"""

from __future__ import annotations

import pytest

from app.formats import FORMATS, Format, Medium, SafeArea, Unit, UnknownFormatError


# ------------------------------------------------------------- the catalogue
def test_the_catalogue_covers_every_medium():
    counts = {medium: len(FORMATS.by_medium(medium)) for medium in Medium}
    assert counts[Medium.PRINT] >= 20
    assert counts[Medium.SOCIAL] >= 12
    assert counts[Medium.VIDEO] >= 6


def test_no_two_formats_share_an_id():
    ids = [item.id for item in FORMATS.all()]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    ("format_id", "width_mm", "height_mm"),
    [
        ("a0", 841, 1189),
        ("a3", 297, 420),
        ("a4", 210, 297),
        ("a6", 105, 148),
        ("us_letter", 215.9, 279.4),
        ("business_card", 90, 50),
        ("album_sleeve", 305, 305),
    ],
)
def test_the_print_sizes_are_the_published_ones(format_id, width_mm, height_mm):
    item = FORMATS.get(format_id)
    assert item is not None
    assert item.width_mm == pytest.approx(width_mm, abs=0.1)
    assert item.height_mm == pytest.approx(height_mm, abs=0.1)


@pytest.mark.parametrize(
    ("format_id", "width_px", "height_px", "aspect"),
    [
        ("instagram_post", 1080, 1080, "1:1"),
        ("instagram_story", 1080, 1920, "9:16"),
        ("instagram_portrait", 1080, 1350, "4:5"),
        ("youtube_thumbnail", 1280, 720, "16:9"),
        ("video_1080p", 1920, 1080, "16:9"),
        ("video_4k", 3840, 2160, "16:9"),
        ("video_vertical", 1080, 1920, "9:16"),
    ],
)
def test_the_screen_sizes_are_the_platform_ones(format_id, width_px, height_px, aspect):
    item = FORMATS.get(format_id)
    assert item is not None
    assert (item.width_px, item.height_px) == (width_px, height_px)
    assert item.aspect_label() == aspect


def test_an_a_series_page_reports_a_ratio_a_designer_recognises():
    """2480:3508 tells nobody anything."""
    assert FORMATS.get("a4").aspect_label() == "0.707:1"
    assert FORMATS.get("instagram_post").aspect_label() == "1:1"


def test_a_vertical_social_format_reserves_the_platform_furniture():
    story = FORMATS.get("instagram_story")
    assert not story.safe_area.is_empty()
    x, y, width, height = story.safe_area.inset_px(story.width_px, story.height_px)
    assert x > 0 and y > 0
    assert height < story.height_px * 0.7, "the caption and buttons need room"


def test_video_formats_carry_a_frame_rate_and_a_duration():
    for item in FORMATS.by_medium(Medium.VIDEO):
        assert item.fps > 0, item.id
        assert item.duration_seconds > 0, item.id


# --------------------------------------------------------------- resolving
@pytest.mark.parametrize(
    ("request_text", "width_mm", "height_mm"),
    [
        ("A3", 297, 420),
        ("a3", 297, 420),
        ("A3 landscape", 420, 297),
        ("A4 portrait", 210, 297),
        ("70x100 cm", 700, 1000),
        ("70 × 100 cm", 700, 1000),
        ("6x3 m", 6000, 3000),
        ("8.5in x 11in", 215.9, 279.4),
        ("8.5 x 11 in", 215.9, 279.4),
        ("210x297", 210, 297),
        ("۲۰۰x۳۰۰", 200, 300),
    ],
)
def test_a_size_can_be_asked_for_in_any_unit(request_text, width_mm, height_mm):
    item = FORMATS.resolve(request_text)
    assert item.width_mm == pytest.approx(width_mm, abs=0.2)
    assert item.height_mm == pytest.approx(height_mm, abs=0.2)


@pytest.mark.parametrize(
    ("request_text", "format_id"),
    [
        ("Instagram story", "instagram_story"),
        ("ig_story", "instagram_story"),
        ("reel", "video_vertical"),
        ("thumbnail", "youtube_thumbnail"),
        ("business card", "business_card"),
        ("billboard 6x3 m", "billboard_6x3"),
    ],
)
def test_a_named_format_is_found_by_any_of_its_names(request_text, format_id):
    assert FORMATS.resolve(request_text).id == format_id


def test_pixels_stay_pixels():
    item = FORMATS.resolve("1080 x 1350 px")
    assert item.unit is Unit.PX
    assert (item.width_px, item.height_px) == (1080, 1350)
    assert item.medium is Medium.SCREEN


def test_mixed_units_are_refused_rather_than_guessed():
    with pytest.raises(UnknownFormatError, match="mixes units"):
        FORMATS.resolve("5 in x 10 cm")


@pytest.mark.parametrize("bad", ["", "   ", "a size", "0x0 mm", "-5x10 cm"])
def test_nonsense_is_refused_with_something_to_act_on(bad):
    with pytest.raises(UnknownFormatError) as caught:
        FORMATS.resolve(bad)
    assert caught.value.recovery_action


def test_a_custom_format_can_be_built_from_numbers():
    item = FORMATS.custom(320, 450, Unit.MM, name="House poster", bleed_mm=5.0)
    assert item.medium is Medium.PRINT
    assert item.bleed_mm == 5.0
    assert item.width_px == pytest.approx(320 / 25.4 * 300, abs=1)


def test_a_custom_video_format_is_recognised_as_video():
    item = FORMATS.custom(1440, 1440, Unit.PX, fps=30, duration_seconds=12)
    assert item.medium is Medium.VIDEO
    assert item.fps == 30


def test_the_resolution_can_be_overridden_without_changing_the_size():
    at_150 = FORMATS.resolve("A2", dpi=150)
    assert at_150.width_mm == pytest.approx(420, abs=0.1)
    assert at_150.width_px == pytest.approx(420 / 25.4 * 150, abs=1)


def test_searching_finds_formats_by_part_of_a_name():
    hits = {item.id for item in FORMATS.search("instagram")}
    assert "instagram_story" in hits and "instagram_post" in hits


def test_a_format_round_trips_through_its_dictionary():
    for item in FORMATS.all():
        data = item.to_dict()
        assert data["width_px"] > 0 and data["height_px"] > 0
        assert data["orientation"] in ("portrait", "landscape", "square")


def test_rotating_a_format_swaps_its_sides():
    story = FORMATS.get("instagram_story").rotated()
    assert (story.width_px, story.height_px) == (1920, 1080)


def test_a_safe_area_survives_a_change_of_resolution():
    """It is a fraction of the frame, not a pixel count."""
    area = SafeArea(top=0.1, bottom=0.2, left=0.05, right=0.05)
    small = area.inset_px(1000, 1000)
    large = area.inset_px(2000, 2000)
    assert large == tuple(value * 2 for value in small)


def test_a_format_with_no_safe_area_says_so():
    assert FORMATS.get("a4").safe_area.is_empty()


def test_the_registry_can_be_described_for_diagnostics():
    summary = FORMATS.describe()
    assert summary["formats"] == len(FORMATS.all())
    assert summary["by_medium"]["print"] > 0


def test_a_format_added_at_runtime_is_findable():
    registry = type(FORMATS)()
    house = Format(
        id="house_special",
        name="House special",
        medium=Medium.PRINT,
        width=333,
        height=444,
        unit=Unit.MM,
        aliases=("special",),
    )
    registry.add(house)
    assert registry.resolve("special").id == "house_special"
