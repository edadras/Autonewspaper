"""Fixtures for the multi-agent studio tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from app.agents.studio_tools import StudioContext
from app.formats.registry import FormatRegistry


@pytest.fixture
def offline_adobe() -> MagicMock:
    """An Adobe service that reports nothing installed.

    The studio must produce real files on a machine with no Adobe at all;
    every test here runs against that machine so nothing can pass by
    accidentally reaching a host that is not there.
    """
    adobe = MagicMock()
    adobe.app.return_value.installed = False
    return adobe


@pytest.fixture
def studio_context(tmp_path: Path, offline_adobe: MagicMock, template) -> StudioContext:
    """A studio working in a throw-away directory."""
    return StudioContext(
        workspace=tmp_path / "studio",
        adobe=offline_adobe,
        formats=FormatRegistry(),
        style_template=template,
        language="fa",
    )


@pytest.fixture
def photograph(tmp_path: Path) -> Path:
    """A picture big enough to print, with a subject on a backdrop.

    A flat rectangle would pass most of these tests and fail the one that
    matters: a cut-out has to have something to cut out.
    """
    target = tmp_path / "photograph.jpg"
    picture = Image.new("RGB", (2400, 1600), (233, 231, 227))
    draw = ImageDraw.Draw(picture)
    draw.ellipse((900, 300, 1500, 1000), fill=(62, 42, 32))
    draw.polygon([(820, 1600), (1200, 950), (1580, 1600)], fill=(28, 52, 92))
    picture.save(target)
    return target
