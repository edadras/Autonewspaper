"""Creative direction: several real concepts, not one safe answer.

The promise this module makes is not "three ideas come back" - that is easy
and worthless. It is that the three are *genuinely different*, that each is
readable, and that each is actually buildable. All three are measurable, so
all three are measured here rather than asserted in a docstring.
"""

from __future__ import annotations

import itertools
import random
from pathlib import Path

import pytest
from PIL import Image

from app.agents.direction import (
    MIN_ACCENT_FROM_INK,
    MIN_DISTINCTNESS,
    MIN_INK_CONTRAST,
    MIN_PALETTE_GAP,
    Approach,
    CreativeDirection,
    Direction,
    _colour_gap,
    _palette_distance,
    contact_sheet,
    distinctness,
    prune,
    spread,
)
from app.agents.recipes import Palette
from app.agents.studio import Brief
from app.creative.style import StyleBrief, contrast_ratio


@pytest.fixture
def director(studio_context) -> CreativeDirection:
    return CreativeDirection(formats=studio_context.formats)


@pytest.fixture
def two_pictures(tmp_path: Path) -> list[str]:
    first = tmp_path / "scene.jpg"
    Image.new("RGB", (1400, 900), (28, 44, 88)).save(first)
    second = tmp_path / "subject.jpg"
    Image.new("RGB", (900, 1200), (232, 230, 226)).save(second)
    return [str(first), str(second)]


def _any_colour() -> str:
    """A colour nobody chose, to make sure the properties hold for all of them."""
    return f"#{random.randrange(0xFFFFFF):06x}"


def _brief(**fields) -> Brief:
    base = {
        "request": "a poster for the film festival",
        "title": "festival",
        "format": "A6",
        "content": "(THIRTY FILMS)\nCITY FILM FESTIVAL\nseven days in October",
        "language": "en",
    }
    base.update(fields)
    return Brief(**base)


# --------------------------------------------------------- distinctness ---


def test_two_concepts_of_the_same_approach_are_not_distinct() -> None:
    """Three colourways of one layout is one idea, not three."""
    left = Direction(
        name="A", approach=Approach.PHOTOGRAPHIC, idea="x",
        palette=Palette(ground="#101828", ink="#ffffff", accent="#c2410c"),
        treatment="t", surface="grain",
    )
    right = Direction(
        name="B", approach=Approach.PHOTOGRAPHIC, idea="y",
        palette=Palette(ground="#131c2e", ink="#ffffff", accent="#c94a15"),
        treatment="t", surface="grain",
    )

    assert distinctness(left, right) < MIN_DISTINCTNESS


def test_two_approaches_are_distinct_however_they_are_coloured() -> None:
    same = Palette(ground="#101828", ink="#ffffff", accent="#c2410c")
    left = Direction(name="A", approach=Approach.PHOTOGRAPHIC, idea="x", palette=same, treatment="a")
    right = Direction(name="B", approach=Approach.EDITORIAL, idea="y", palette=same, treatment="b")

    assert distinctness(left, right) >= MIN_DISTINCTNESS


def test_a_set_is_only_as_distinct_as_its_closest_pair() -> None:
    """Four concepts where two are the same idea is a set of three."""
    palette = Palette(ground="#101828", ink="#ffffff", accent="#c2410c")
    directions = [
        Direction(name=str(index), approach=approach, idea="x", palette=palette, treatment="t")
        for index, approach in enumerate(
            (Approach.PHOTOGRAPHIC, Approach.EDITORIAL, Approach.PHOTOGRAPHIC)
        )
    ]

    assert spread(directions) < MIN_DISTINCTNESS
    assert spread(directions[:2]) >= MIN_DISTINCTNESS


def test_a_concept_too_like_one_already_on_the_table_is_dropped() -> None:
    palette = Palette(ground="#101828", ink="#ffffff", accent="#c2410c")
    directions = [
        Direction(name="first", approach=Approach.GRAPHIC, idea="x", palette=palette, treatment="t"),
        Direction(name="echo", approach=Approach.GRAPHIC, idea="y", palette=palette, treatment="t"),
        Direction(name="other", approach=Approach.EDITORIAL, idea="z", palette=palette, treatment="u"),
    ]

    kept = prune(directions)

    assert [item.name for item in kept] == ["first", "other"]


# ------------------------------------------------------------ proposing ---


def test_the_concepts_that_come_back_are_actually_different(director, two_pictures) -> None:
    directions = director.propose(_brief(references=two_pictures), count=4)

    assert len(directions) == 4
    assert spread(directions) >= MIN_DISTINCTNESS
    assert len({item.approach for item in directions}) == 4


def test_exactly_one_concept_is_recommended(director, two_pictures) -> None:
    """A director who will not say which one has not directed anything."""
    directions = director.propose(_brief(references=two_pictures), count=3)

    assert sum(1 for item in directions if item.recommended) == 1


def test_an_approach_that_needs_a_picture_is_not_proposed_without_one(director) -> None:
    directions = director.propose(_brief(references=[]), count=4)

    approaches = {item.approach for item in directions}
    assert Approach.PHOTOGRAPHIC not in approaches
    assert Approach.COMPOSITE not in approaches


def test_two_pictures_put_the_built_cover_on_the_table(director, two_pictures) -> None:
    directions = director.propose(_brief(references=two_pictures), count=3)

    assert Approach.COMPOSITE in {item.approach for item in directions}
    assert directions[0].approach is Approach.COMPOSITE, "and it is the one to run"


def test_every_concept_can_actually_be_built(director, two_pictures) -> None:
    """A concept with no recipe is a sentence, not a proposal."""
    directions = director.propose(_brief(references=two_pictures), count=4)

    for item in directions:
        assert item.recipe is not None, item.name
        assert item.recipe.calls
        assert item.recipe.calls[0][0] == "start_design"


def test_the_rationale_names_something_in_this_brief(director, two_pictures) -> None:
    """One that would read the same for any brief is not a rationale."""
    with_copy = director.propose(
        _brief(references=two_pictures, content="A HEADLINE\n\n" + "copy " * 90), count=4
    )
    editorial = next(
        (item for item in with_copy if item.approach is Approach.EDITORIAL), None
    )
    composite = next(item for item in with_copy if item.approach is Approach.COMPOSITE)

    assert "2 pictures" in composite.rationale
    if editorial is not None:
        assert any(character.isdigit() for character in editorial.rationale)


def test_asking_for_one_concept_gives_one(director, two_pictures) -> None:
    assert len(director.propose(_brief(references=two_pictures), count=1)) == 1


def test_asking_for_more_than_there_are_approaches_does_not_repeat_one(
    director, two_pictures
) -> None:
    directions = director.propose(_brief(references=two_pictures), count=5)

    approaches = [item.approach for item in directions]
    assert len(approaches) == len(set(approaches))


# --------------------------------------------------------------- colour ---


@pytest.mark.parametrize("seed", range(6))
def test_the_palettes_hold_up_whatever_the_references_say(director, two_pictures, seed) -> None:
    """The properties have to hold for any brief, not for the one in the demo."""
    random.seed(seed)
    style = StyleBrief(
        background=_any_colour(),
        foreground=_any_colour(),
        accent=_any_colour(),
    )

    directions = director.propose(_brief(references=two_pictures), style=style, count=4)

    assert spread(directions) >= MIN_DISTINCTNESS
    for item in directions:
        assert contrast_ratio(item.palette.ground, item.palette.ink) >= MIN_INK_CONTRAST, (
            f"{item.name}: type nobody can read on {item.palette.ground}"
        )
    for left, right in itertools.combinations(directions, 2):
        assert _palette_distance(left.palette, right.palette) >= MIN_PALETTE_GAP, (
            f"{left.name} and {right.name} are one palette twice"
        )


def test_the_accent_stays_a_second_colour(director, two_pictures) -> None:
    """An accent lightened until it reads as the type is not an accent."""
    style = StyleBrief(background="#0a0a0a", foreground="#ffffff", accent="#f5f5f5")

    directions = director.propose(_brief(references=two_pictures), style=style, count=4)

    for item in directions:
        assert _colour_gap(item.palette.accent, item.palette.ink) >= MIN_ACCENT_FROM_INK, item.name


def test_a_mid_tone_ground_is_moved_rather_than_left_unreadable(director) -> None:
    """Neither white nor near-black reads on mid-grey, so the ground gives way."""
    style = StyleBrief(background="#808080", foreground="#7f7f7f", accent="#818181")

    directions = director.propose(_brief(), style=style, count=3)

    for item in directions:
        assert contrast_ratio(item.palette.ground, item.palette.ink) >= MIN_INK_CONTRAST


# -------------------------------------------------------- contact sheet ---


def test_the_concepts_are_pinned_up_side_by_side(director, two_pictures, tmp_path) -> None:
    directions = director.propose(_brief(references=two_pictures), count=3)
    for index, item in enumerate(directions):
        preview = tmp_path / f"c{index}.png"
        Image.new("RGB", (600, 850), (40 + index * 60, 50, 90)).save(preview)
        item.preview = str(preview)

    sheet = contact_sheet(directions, tmp_path / "sheet.png", title="Festival")

    assert sheet.exists()
    with Image.open(sheet) as image:
        assert image.width > 600 * 2, "they are laid out beside each other, not stacked"
        assert image.height > 850 * 0.4


def test_pinning_up_concepts_nobody_rendered_says_so(director, two_pictures, tmp_path) -> None:
    directions = director.propose(_brief(references=two_pictures), count=2)

    with pytest.raises(ValueError, match="rendered"):
        contact_sheet(directions, tmp_path / "sheet.png")


# --------------------------------------------------------- the whole job --


@pytest.fixture
def photoshop_studio(studio_context, tmp_path):
    """A studio whose Photoshop controller is real but has no Photoshop."""
    from app.adobe.detect import detect_photoshop
    from app.adobe.photoshop.controller import PhotoshopController

    studio_context.adobe.photoshop = PhotoshopController(
        tmp_path / "psd", app=detect_photoshop()
    )
    return studio_context


def test_the_concepts_are_built_not_described(photoshop_studio, two_pictures) -> None:
    """A director presents work. Three paragraphs about three ideas is not it."""
    from app.agents.studio import Studio

    run = Studio(photoshop_studio).direct(
        _brief(references=two_pictures, title="Festival"), count=3
    )

    assert run.stop_reason == "concepts ready"
    assert len(run.directions) == 3
    assert all(result.ok for result in run.results), [
        result.error or result.skipped for result in run.results if not result.ok
    ]
    for direction in run.directions:
        assert direction.preview, f"{direction.name} was never rendered"
        assert Path(direction.preview).exists()


def test_the_built_concepts_actually_look_different(photoshop_studio, two_pictures) -> None:
    """The measurement that matters is on the pixels, not on the plan."""
    from app.agents.studio import Studio
    from app.creative.reference import measure_image

    run = Studio(photoshop_studio).direct(_brief(references=two_pictures), count=3)

    grounds = []
    for direction in run.directions:
        measured = measure_image(direction.preview, faces=False)
        grounds.append(measured.palette[0].hex if measured.palette else "#000000")
    for left, right in itertools.combinations(grounds, 2):
        assert _colour_gap(left, right) > 24, f"{grounds} - these read as one design"


def test_they_are_pinned_up_and_the_choice_is_offered(photoshop_studio, two_pictures) -> None:
    from app.agents.studio import Studio

    run = Studio(photoshop_studio).direct(_brief(references=two_pictures), count=3)

    assert run.sheet and Path(run.sheet).exists()
    card = next(question for question in run.questions if question.id == "concept")
    assert len(card.options) == 3
    assert sum(1 for option in card.options if option.recommended) == 1


def test_one_concept_is_not_offered_as_a_choice(photoshop_studio, two_pictures) -> None:
    """Asking which of one is which is not a question."""
    from app.agents.studio import Studio

    run = Studio(photoshop_studio).direct(_brief(references=two_pictures), count=1)

    assert not run.questions


def test_the_run_survives_being_serialised(photoshop_studio, two_pictures) -> None:
    import json

    from app.agents.studio import Studio

    run = Studio(photoshop_studio).direct(_brief(references=two_pictures), count=2)

    payload = json.loads(json.dumps(run.to_dict(), ensure_ascii=False, default=str))

    assert len(payload["directions"]) == 2
    assert payload["sheet"]
    assert payload["directions"][0]["palette"]["accent"]
