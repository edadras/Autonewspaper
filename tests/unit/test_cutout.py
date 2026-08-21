"""Lifting a subject off its background without Photoshop.

Photoshop's own selection is better and the studio uses it when Photoshop is
there. These tests pin what happens when it is not: a clean cut on the case
that can be cut, and a plain refusal on the case that cannot, because a
mangled subject on a front page is worse than no cut-out at all.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

from app.utils import imaging


def _subject_on_a_backdrop(target: Path, size: tuple[int, int] = (800, 600)) -> Path:
    """A dark subject against an even, light background."""
    picture = Image.new("RGB", size, (233, 231, 227))
    draw = ImageDraw.Draw(picture)
    draw.ellipse(
        (size[0] * 0.35, size[1] * 0.2, size[0] * 0.65, size[1] * 0.7), fill=(62, 42, 32)
    )
    draw.polygon(
        [(size[0] * 0.3, size[1]), (size[0] * 0.5, size[1] * 0.6), (size[0] * 0.7, size[1])],
        fill=(28, 52, 92),
    )
    picture.save(target)
    return target


def test_a_subject_is_lifted_off_an_even_backdrop(tmp_path: Path) -> None:
    source = _subject_on_a_backdrop(tmp_path / "subject.jpg")

    result = imaging.cut_out_subject(source, tmp_path / "cut.png")

    assert result["cut_out"]
    with Image.open(result["path"]) as image:
        picture = image.convert("RGBA")
        assert picture.getpixel((3, 3))[3] == 0, "the corner is background"
        assert picture.getpixel((picture.width // 2, int(picture.height * 0.45)))[3] == 255


def test_a_pale_patch_inside_the_subject_is_not_eaten(tmp_path: Path) -> None:
    """Only background connected to the edge is background."""
    source = _subject_on_a_backdrop(tmp_path / "subject.jpg")
    with Image.open(source) as opened:
        picture = opened.convert("RGB")
    draw = ImageDraw.Draw(picture)
    draw.ellipse((380, 260, 420, 300), fill=(233, 231, 227))  # a badge on the chest
    picture.save(source)

    result = imaging.cut_out_subject(source, tmp_path / "cut.png")

    assert result["cut_out"]
    with Image.open(result["path"]) as image:
        assert image.convert("RGBA").getpixel((400, 280))[3] == 255


def test_a_busy_background_is_refused_rather_than_mangled(tmp_path: Path) -> None:
    picture = Image.new("RGB", (300, 220))
    pixels = picture.load()
    random.seed(11)
    for y in range(220):
        for x in range(300):
            pixels[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    source = tmp_path / "busy.png"
    picture.save(source)

    result = imaging.cut_out_subject(source, tmp_path / "cut.png")

    assert not result["cut_out"]
    assert result["path"] == str(source), "the original is left alone"
    assert "Photoshop" in result["reason"]


def test_a_picture_with_no_subject_at_all_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "flat.png"
    Image.new("RGB", (200, 150), (200, 200, 200)).save(source)

    result = imaging.cut_out_subject(source, tmp_path / "cut.png")

    assert not result["cut_out"]
    assert "100%" in result["reason"]


def test_the_photograph_pipeline_keeps_the_transparency_it_made(tmp_path: Path) -> None:
    """open_image flattens by design; a cut-out must not be round-tripped through it."""
    from app.adobe.detect import detect_photoshop
    from app.adobe.photoshop.controller import PhotoshopController

    source = _subject_on_a_backdrop(tmp_path / "subject.jpg", size=(1200, 900))
    controller = PhotoshopController(tmp_path / "work", app=detect_photoshop())

    result = controller.process_image(
        source, tmp_path / "out.png", aspect=0.8, remove_background=True
    )

    with Image.open(result["path"]) as image:
        assert image.mode == "RGBA"
    assert any("locally" in note for note in result["notes"])


def test_a_photograph_that_is_not_cut_out_stays_flat(tmp_path: Path) -> None:
    from app.adobe.detect import detect_photoshop
    from app.adobe.photoshop.controller import PhotoshopController

    source = _subject_on_a_backdrop(tmp_path / "subject.jpg")
    controller = PhotoshopController(tmp_path / "work", app=detect_photoshop())

    result = controller.process_image(source, tmp_path / "out.jpg", aspect=1.5)

    with Image.open(result["path"]) as image:
        assert image.mode == "RGB"
    assert Path(result["path"]).suffix == ".jpg", "it stays the format that was asked for"
