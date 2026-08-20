"""Reading a reference the user attached.

The point of the split is that measurable things are measured: a picture that
is blue must not become a warm brief because a model said "golden hour". So
the measurements are checked against images built to have known properties,
and the model's contribution is checked for being kept in its place.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.creative import (
    ReferenceAnalyst,
    brief_from_measurements,
    contrast_ratio,
    measure_image,
    merge_briefs,
    readable_on,
)
from app.creative.analyst import Reference
from app.creative.reference import Swatch, extract_palette
from app.creative.style import StyleBrief, relative_luminance, shift


def _flat(path, color, size=(400, 300)):
    Image.new("RGB", size, color).save(path)
    return path


def _two_tone(path, left, right, size=(400, 300)):
    image = Image.new("RGB", size, left)
    ImageDraw.Draw(image).rectangle([size[0] // 2, 0, size[0], size[1]], fill=right)
    image.save(path)
    return path


def _busy(path, size=(400, 300), step=4):
    image = Image.new("RGB", size, "#ffffff")
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], step):
        draw.line([(x, 0), (x, size[1])], fill="#000000")
    image.save(path)
    return path


# ------------------------------------------------------------ measurements
def test_a_warm_picture_measures_warm_and_a_cold_one_cold(tmp_path):
    warm = measure_image(_flat(tmp_path / "warm.png", "#e07b39"), faces=False)
    cold = measure_image(_flat(tmp_path / "cold.png", "#3970e0"), faces=False)

    assert warm.warmth > 20
    assert cold.warmth < -20


def test_brightness_and_contrast_follow_the_pixels(tmp_path):
    dark = measure_image(_flat(tmp_path / "dark.png", "#101010"), faces=False)
    light = measure_image(_flat(tmp_path / "light.png", "#f2f2f2"), faces=False)
    split = measure_image(_two_tone(tmp_path / "split.png", "#000000", "#ffffff"), faces=False)

    assert dark.brightness < 15 and light.brightness > 85
    assert dark.contrast < 3, "a flat field has no contrast"
    assert split.contrast > 40, "black against white has a lot"


def test_a_grey_picture_is_recognised_as_monochrome(tmp_path):
    grey = measure_image(_two_tone(tmp_path / "grey.png", "#333333", "#cccccc"), faces=False)
    assert grey.is_monochrome
    assert grey.saturation < 8


def test_a_busy_frame_measures_busy_and_an_empty_one_does_not(tmp_path):
    busy = measure_image(_busy(tmp_path / "busy.png"), faces=False)
    empty = measure_image(_flat(tmp_path / "empty.png", "#ffffff"), faces=False)

    assert busy.busyness > empty.busyness + 15
    assert empty.empty_share > 0.9
    assert busy.empty_share < 0.4


def test_the_weight_of_a_composition_is_found(tmp_path):
    image = Image.new("RGB", (400, 400), "#ffffff")
    ImageDraw.Draw(image).rectangle([0, 0, 120, 400], fill="#000000")
    image.save(tmp_path / "left.png")

    measured = measure_image(tmp_path / "left.png", faces=False)
    assert measured.weight_x < 0.3, "the ink is on the left"
    assert 0.4 < measured.weight_y < 0.6


def test_the_description_matches_the_numbers(tmp_path):
    measured = measure_image(_flat(tmp_path / "dark.png", "#0a0a1e"), faces=False)
    words = measured.describe()
    assert "dark" in words
    assert "cool" in words or "neutral" in words


# ---------------------------------------------------------------- palettes
def test_the_palette_is_the_colours_the_picture_is_made_of(tmp_path):
    image = Image.new("RGB", (300, 300), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 300, 150], fill="#113463")
    draw.rectangle([0, 150, 150, 300], fill="#bb4210")
    image.save(tmp_path / "three.png")

    palette = measure_image(tmp_path / "three.png", faces=False).palette
    hexes = [swatch.hex for swatch in palette]
    assert len(palette) >= 3
    assert any(h.startswith("#1") or h.startswith("#0") for h in hexes), "the navy"
    assert any(h.startswith("#b") or h.startswith("#c") for h in hexes), "the orange"
    assert abs(sum(s.share for s in palette) - 1.0) < 0.35


def test_near_identical_colours_are_merged_into_one_swatch(tmp_path):
    """Six near-identical blues for a picture of the sea is true and useless."""
    image = Image.new("RGB", (300, 300))
    draw = ImageDraw.Draw(image)
    for index, shade in enumerate(("#2b5c9e", "#2d5ea0", "#2f60a2", "#31629f")):
        draw.rectangle([0, index * 75, 300, (index + 1) * 75], fill=shade)
    image.save(tmp_path / "sea.png")

    palette = extract_palette(Image.open(tmp_path / "sea.png"))
    assert len(palette) <= 2


def test_every_swatch_is_given_the_job_it_does(tmp_path):
    image = Image.new("RGB", (300, 300), "#f4f4f4")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 300, 200], fill="#123a6b")
    draw.rectangle([10, 250, 60, 290], fill="#ff4b1f")
    image.save(tmp_path / "roles.png")

    roles = {swatch.role for swatch in measure_image(tmp_path / "roles.png", faces=False).palette}
    assert "dominant" in roles
    assert "accent" in roles, "the small saturated patch is what punctuates the design"


def test_a_neutral_swatch_knows_it_is_neutral():
    assert Swatch(hex="#808080", share=0.5).is_neutral()
    assert not Swatch(hex="#c2410c", share=0.5).is_neutral()


# ------------------------------------------------------------------ briefs
def test_a_dark_reference_produces_a_dark_ground(tmp_path):
    image = Image.new("RGB", (300, 300), "#0d1117")
    ImageDraw.Draw(image).rectangle([40, 40, 260, 130], fill="#e6edf3")
    image.save(tmp_path / "dark.png")

    brief = brief_from_measurements(measure_image(tmp_path / "dark.png", faces=False))
    assert relative_luminance(brief.background) < 0.2
    assert brief.readable(), "text still has to be readable on it"


def test_a_bright_reference_produces_a_bright_ground(tmp_path):
    brief = brief_from_measurements(measure_image(_flat(tmp_path / "l.png", "#fafafa"), faces=False))
    assert relative_luminance(brief.background) > 0.7
    assert brief.readable()


def test_the_ground_and_the_text_can_always_be_read(tmp_path):
    for colour in ("#000000", "#ffffff", "#c2410c", "#113463", "#7f7f7f", "#00ff00"):
        brief = brief_from_measurements(measure_image(_flat(tmp_path / "c.png", colour), faces=False))
        assert brief.contrast_ratio() >= 4.5, f"{colour} produced an unreadable pairing"


def test_a_busy_reference_gets_quieter_type_than_an_empty_one(tmp_path):
    busy = brief_from_measurements(measure_image(_busy(tmp_path / "b.png"), faces=False))
    calm = brief_from_measurements(measure_image(_flat(tmp_path / "c.png", "#f0f0f0"), faces=False))

    assert busy.density == "dense" and calm.density == "airy"
    assert busy.typography.scale_ratio < calm.typography.scale_ratio
    assert busy.typography.justified and not calm.typography.justified


def test_the_type_scale_steps_by_the_chosen_ratio():
    brief = StyleBrief()
    brief.typography.scale_ratio = 1.5
    sizes = brief.typography.sizes(10, steps=4)
    assert sizes == [10.0, 15.0, 22.5, 33.75]


def test_an_accent_that_could_not_be_seen_is_pushed_until_it_can(tmp_path):
    """An accent nobody can pick out is not an accent."""
    image = Image.new("RGB", (300, 300), "#101010")
    ImageDraw.Draw(image).rectangle([10, 10, 40, 40], fill="#141418")
    image.save(tmp_path / "murk.png")

    brief = brief_from_measurements(measure_image(tmp_path / "murk.png", faces=False))
    assert contrast_ratio(brief.accent, brief.background) >= 2.0


def test_several_references_merge_into_one_brief(tmp_path):
    first = brief_from_measurements(
        measure_image(_flat(tmp_path / "a.png", "#e07b39"), faces=False), source="a.png"
    )
    second = brief_from_measurements(
        measure_image(_flat(tmp_path / "b.png", "#e08b49"), faces=False), source="b.png"
    )
    merged = merge_briefs([first, second])

    assert merged.sources == ["a.png", "b.png"]
    assert merged.temperature == "warm"
    assert len(merged.palette) >= 1


def test_merging_nothing_is_not_an_error():
    assert merge_briefs([]).background == "#ffffff"


@pytest.mark.parametrize(
    ("background", "expected"),
    [("#ffffff", "#111111"), ("#000000", "#ffffff"), ("#123a6b", "#ffffff"), ("#f4e7c3", "#111111")],
)
def test_the_readable_colour_is_chosen_for_the_ground(background, expected):
    assert readable_on(background) == expected


def test_a_colour_can_be_nudged_without_leaving_the_gamut():
    lighter = shift("#808080", lighten=0.3)
    assert relative_luminance(lighter) > relative_luminance("#808080")
    assert shift("#ffffff", lighten=0.9) == "#ffffff", "it cannot go past white"
    assert shift("not a colour", lighten=0.2) == "not a colour"


# ----------------------------------------------------------- the whole read
def test_a_set_of_attachments_becomes_one_brief(tmp_path):
    _flat(tmp_path / "one.png", "#123a6b")
    _flat(tmp_path / "two.png", "#1a4479")

    result = ReferenceAnalyst(ai=None).analyze([tmp_path / "one.png", tmp_path / "two.png"])

    assert len(result.usable) == 2
    assert not result.failures
    assert result.brief.temperature == "cool"
    assert "no vision model" in result.brief.notes


def test_an_attachment_that_cannot_be_read_does_not_lose_the_others(tmp_path):
    _flat(tmp_path / "good.png", "#123a6b")
    (tmp_path / "notes.json").write_text("{}")

    result = ReferenceAnalyst(ai=None).analyze(
        [tmp_path / "good.png", tmp_path / "gone.png", tmp_path / "notes.json"]
    )

    assert [Reference.usable.fget(r) for r in result.references] == [True, False, False]  # type: ignore[attr-defined]
    reasons = [r.error for r in result.failures]
    assert "does not exist" in reasons[0]
    assert ".json" in reasons[1]
    assert result.brief.palette, "the readable reference still produced a brief"


def test_the_brief_survives_a_round_trip_to_json(tmp_path):
    result = ReferenceAnalyst(ai=None).analyze([_flat(tmp_path / "a.png", "#123a6b")])
    import json

    data = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert data["brief"]["background"]
    assert data["references"][0]["measurements"]["palette"]
