"""Fixtures for the multi-agent studio tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

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
    """A picture big enough to print."""
    target = tmp_path / "photograph.jpg"
    Image.new("RGB", (2400, 1600), (26, 46, 88)).save(target)
    return target
