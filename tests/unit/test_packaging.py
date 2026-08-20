"""What the installer ships.

The frozen Windows build reads its prompts, templates, JSX runtime and icons
from the bundle rather than from the source tree. Nothing else in the suite
runs frozen, so a data file dropped from the specification would only show up
as a broken installation - these tests compare what the application asks for
against what the specification packs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "installer" / "ai_newspaper_studio.spec"


def _datas() -> list[tuple[str, str]]:
    """The ``datas`` list of the specification, as (source, destination)."""
    source = SPEC.read_text(encoding="utf-8")
    match = re.search(r"^datas = (\[.*?^\])", source, re.MULTILINE | re.DOTALL)
    assert match, "the specification no longer declares a literal 'datas' list"
    # The entries are str(ROOT / "x" / "y") calls; reduce them to their parts.
    entries: list[tuple[str, str]] = []
    for element in ast.parse(match.group(1), mode="eval").body.elts:
        source_expr, destination = element.elts
        parts: list[str] = []
        node = source_expr.args[0] if isinstance(source_expr, ast.Call) else source_expr
        while isinstance(node, ast.BinOp):
            parts.insert(0, node.right.value)
            node = node.left
        entries.append(("/".join(parts), destination.value))
    return entries


@pytest.fixture(scope="module")
def datas():
    return _datas()


def test_every_packed_path_exists(datas):
    for source, _destination in datas:
        assert (ROOT / source).exists(), f"the specification packs {source}, which is not in the tree"


@pytest.mark.parametrize(
    ("attribute", "packed_as"),
    [("prompts", "prompts"), ("scripts", "app/adobe/scripts"), ("resources", "resources")],
)
def test_the_read_only_paths_are_packed(datas, attribute, packed_as):
    """Each directory ``AppPaths`` resolves under the bundle root is shipped."""
    from app.config.paths import AppPaths

    paths = AppPaths.resolve(ROOT / "unused-data-dir")
    wanted = getattr(paths, attribute).relative_to(paths.root).as_posix()
    assert wanted == packed_as
    assert any(destination.rstrip("/") == packed_as for _source, destination in datas)


def test_the_builtin_templates_are_packed(datas):
    from app.config.paths import AppPaths

    paths = AppPaths.resolve(ROOT / "unused-data-dir")
    wanted = paths.builtin_templates().relative_to(paths.root).as_posix()
    assert any(destination.rstrip("/") == wanted for _source, destination in datas)


def test_the_jsx_runtime_files_are_all_present():
    """A missing library file breaks every InDesign call at run time."""
    from app.adobe.jsx import HOST_LIBRARIES, SCRIPTS_DIR

    for host, names in HOST_LIBRARIES.items():
        for name in names:
            assert (SCRIPTS_DIR / name).exists(), f"{host} needs {name}"


def test_the_frozen_build_looks_for_its_resources_inside_the_bundle(monkeypatch, tmp_path):
    """``sys._MEIPASS`` is the root once PyInstaller has unpacked the bundle."""
    import sys

    from app.config.paths import AppPaths

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    paths = AppPaths.resolve(tmp_path / "data")

    assert paths.root == tmp_path
    assert paths.prompts == tmp_path / "prompts"
    assert paths.scripts == tmp_path / "app" / "adobe" / "scripts"
    assert paths.builtin_templates() == tmp_path / "templates"
    # Writable state still belongs to the user, never inside the bundle.
    assert paths.data == tmp_path / "data"
    assert not paths.projects.is_relative_to(tmp_path / "prompts")


def test_the_installer_script_installs_what_the_specification_builds():
    """The Inno Setup script and the PyInstaller output must agree."""
    setup = (ROOT / "installer" / "setup.iss").read_text(encoding="utf-8")
    assert "AINewspaperStudio" in setup
    # §Deliverables: a Desktop shortcut, a Start Menu entry and an uninstaller.
    assert "{autodesktop}" in setup or "{commondesktop}" in setup
    assert "{autoprograms}" in setup or "{group}" in setup
    assert "UninstallDisplayName" in setup or "uninstall" in setup.lower()
