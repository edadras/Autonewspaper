"""Surface: the grain, the stock and the screen a design is printed on."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.design.textures import (
    BLEND_MODES,
    DEFAULT_OPACITY,
    MAX_GENERATED,
    Texture,
    TextureFactory,
    TextureSpec,
    make_texture,
)


@pytest.mark.parametrize("kind", list(Texture))
def test_every_surface_is_made_at_the_size_asked_for(kind: Texture) -> None:
    texture = make_texture(TextureSpec(kind=kind, width_px=320, height_px=200))

    assert texture.size == (320, 200)
    assert texture.mode == "RGBA"
    texture.close()


@pytest.mark.parametrize("kind", list(Texture))
def test_every_surface_actually_varies(kind: Texture) -> None:
    """A texture that is one value everywhere is a tint, not a surface."""
    texture = make_texture(TextureSpec(kind=kind, width_px=260, height_px=260))

    alpha = texture.getchannel("A")
    values = {value for value, count in enumerate(alpha.histogram()) if count}
    texture.close()

    # Scanlines are meant to be two values - the line and the gap between
    # them; everything else is a continuous surface.
    wanted = 2 if kind is Texture.SCANLINES else 4
    assert len(values) >= wanted, f"{kind.value} is flat"


@pytest.mark.parametrize("kind", list(Texture))
def test_every_surface_has_a_mode_and_a_strength_it_wants(kind: Texture) -> None:
    """A paper texture laid on as a normal layer is a grey rectangle."""
    assert kind in BLEND_MODES
    assert 0 < DEFAULT_OPACITY[kind] <= 100
    spec = TextureSpec(kind=kind, width_px=64, height_px=64)
    assert spec.blend_mode == BLEND_MODES[kind]
    assert spec.strength == DEFAULT_OPACITY[kind]


def test_the_same_surface_twice_is_the_same_surface() -> None:
    """A design rebuilt must be the design, not a new roll of the dice."""
    spec = TextureSpec(kind=Texture.GRAIN, width_px=180, height_px=180, seed="poster")

    first = make_texture(spec)
    second = make_texture(spec)

    assert first.tobytes() == second.tobytes()
    first.close()
    second.close()


def test_a_different_seed_gives_a_different_surface() -> None:
    left = make_texture(TextureSpec(kind=Texture.GRAIN, width_px=160, height_px=160, seed="a"))
    right = make_texture(TextureSpec(kind=Texture.GRAIN, width_px=160, height_px=160, seed="b"))

    assert left.tobytes() != right.tobytes()
    left.close()
    right.close()


def test_a_vignette_darkens_the_corners_and_not_the_middle() -> None:
    """The other way round is not a vignette, it is a stain."""
    texture = make_texture(TextureSpec(kind=Texture.VIGNETTE, width_px=400, height_px=400))
    alpha = texture.getchannel("A")

    middle = alpha.getpixel((200, 200))
    corner = min(
        alpha.getpixel(point) for point in ((3, 3), (396, 3), (3, 396), (396, 396))
    )
    texture.close()

    assert corner > middle + 20, f"corner {corner}, middle {middle}"


@pytest.mark.parametrize("kind", [Texture.LINEN, Texture.HALFTONE])
def test_a_rotated_surface_reaches_the_corners(kind: Texture) -> None:
    """Rotating a mask in place leaves four bare triangles."""
    texture = make_texture(TextureSpec(kind=kind, width_px=300, height_px=220, angle=30))
    alpha = texture.getchannel("A")

    corners = [alpha.crop(box) for box in ((0, 0, 30, 30), (270, 190, 300, 220))]
    texture.close()

    for patch in corners:
        assert max(patch.getdata()) > 0, f"{kind.value} leaves a bare corner"
        patch.close()


def test_a_huge_surface_is_not_generated_pixel_for_pixel() -> None:
    """Grain over a hoarding at 300 dpi is seven hundred million pixels."""
    spec = TextureSpec(kind=Texture.GRAIN, width_px=MAX_GENERATED * 2, height_px=600)

    texture = make_texture(spec)

    assert texture.size == (MAX_GENERATED * 2, 600), "it still comes out the size asked for"
    texture.close()


def test_the_strength_carries_in_the_alpha_not_in_the_colour() -> None:
    faint = make_texture(TextureSpec(kind=Texture.NOISE, width_px=120, height_px=120, opacity=10))
    strong = make_texture(TextureSpec(kind=Texture.NOISE, width_px=120, height_px=120, opacity=90))

    assert max(faint.getchannel("A").getdata()) < max(strong.getchannel("A").getdata())
    faint.close()
    strong.close()


def test_the_factory_makes_each_surface_once(tmp_path: Path) -> None:
    factory = TextureFactory(tmp_path)
    spec = TextureSpec(kind=Texture.PAPER, width_px=200, height_px=200)

    first = factory.make(spec)
    second = factory.make(spec)

    assert first == second
    assert first.exists()
    assert factory.describe()["surfaces"] == 1


def test_two_different_surfaces_are_two_files(tmp_path: Path) -> None:
    factory = TextureFactory(tmp_path)

    paper = factory.make(TextureSpec(kind=Texture.PAPER, width_px=200, height_px=200))
    grain = factory.make(TextureSpec(kind=Texture.GRAIN, width_px=200, height_px=200))

    assert paper != grain
    with Image.open(paper) as image:
        assert image.mode == "RGBA"
