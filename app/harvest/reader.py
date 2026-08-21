"""Getting pages out of a publication, whatever form it arrives in.

A PDF carries its own page geometry, which is exact; a folder of scans
carries none, so the operator says how wide the sheet is and everything else
follows from the pixels. Either way what comes out is the same thing: a page
at a known resolution with a known size in millimetres.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.errors import AppError, Component

log = logging.getLogger(__name__)

#: A point is a seventy-second of an inch, and an inch is 25.4 mm.
PT_TO_MM = 25.4 / 72.0

#: Enough to measure a column grid and a keyline; not so much that a
#: twelve-page broadsheet needs a gigabyte to look at.
ANALYSIS_DPI = 110


class HarvestError(AppError):
    """A publication that could not be read."""

    component = Component.CORE
    recovery_action = "Supply a PDF, or page images with the sheet width in millimetres."


@dataclass
class PageRaster:
    """One page of a publication, ready to be measured."""

    index: int
    image: Image.Image
    width_mm: float
    height_mm: float
    dpi: float
    source: str = ""

    @property
    def px_per_mm(self) -> float:
        """How many pixels a millimetre is worth on this page."""
        return self.image.width / max(1e-6, self.width_mm)

    def mm(self, pixels: float) -> float:
        """Convert a measurement in pixels to millimetres."""
        return pixels / max(1e-6, self.px_per_mm)

    def px(self, millimetres: float) -> float:
        """Convert a measurement in millimetres to pixels."""
        return millimetres * self.px_per_mm

    def close(self) -> None:
        """Release the page's pixels."""
        try:
            self.image.close()
        except Exception:  # noqa: BLE001 - closing twice is not worth reporting
            pass


def page_sizes(path: Path | str) -> list[tuple[float, float]]:
    """Every page's trim size in millimetres, read from the PDF itself.

    Exact, and needs nothing installed: the size is in the file. Rotation is
    honoured, because a landscape page stored rotated is still landscape.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    sizes: list[tuple[float, float]] = []
    for page in reader.pages:
        box = page.mediabox
        width = float(box.width) * PT_TO_MM
        height = float(box.height) * PT_TO_MM
        rotation = int(page.get("/Rotate") or 0) % 360
        if rotation in (90, 270):
            width, height = height, width
        sizes.append((round(width, 2), round(height, 2)))
    return sizes


def read_pdf(
    path: Path | str,
    *,
    dpi: int = ANALYSIS_DPI,
    pages: list[int] | None = None,
    limit: int = 24,
) -> list[PageRaster]:
    """Rasterise a publication for measuring.

    The geometry comes from the file and the pixels from poppler. Without
    poppler the sizes can still be read, so the caller is told exactly what is
    missing rather than being handed an empty list.
    """
    source = Path(path)
    if not source.exists():
        raise HarvestError(f"There is no publication at {source}")
    sizes = page_sizes(source)
    if not sizes:
        raise HarvestError(f"{source.name} has no pages")

    wanted = pages or list(range(1, len(sizes) + 1))
    wanted = [index for index in wanted if 1 <= index <= len(sizes)][:limit]
    if not wanted:
        raise HarvestError(
            f"{source.name} has {len(sizes)} page(s); none of the requested ones exist"
        )

    renderer = shutil.which("pdftoppm")
    if renderer is None:
        raise HarvestError(
            "Reading a PDF's pages needs poppler's pdftoppm, which is not on this machine",
            context={"pages": len(sizes), "sizes_mm": sizes[:4]},
            recovery_action=(
                "Install poppler, or export the pages as PNG or JPEG and harvest those."
            ),
        )

    out: list[PageRaster] = []
    with tempfile.TemporaryDirectory(prefix="ains-harvest-") as workspace:
        stem = Path(workspace) / "page"
        for index in wanted:
            command = [
                renderer, "-r", str(dpi), "-f", str(index), "-l", str(index),
                "-png", "-singlefile", str(source), str(stem),
            ]
            result = subprocess.run(  # noqa: S603 - a fixed command with numeric arguments
                command, capture_output=True, text=True, timeout=180, check=False
            )
            rendered = stem.with_suffix(".png")
            if result.returncode != 0 or not rendered.exists():
                log.warning("Page %d could not be rendered: %s", index, result.stderr.strip()[:200])
                continue
            with Image.open(rendered) as opened:
                image = opened.convert("RGB").copy()
            width_mm, height_mm = sizes[index - 1]
            out.append(
                PageRaster(
                    index=index,
                    image=image,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    dpi=float(dpi),
                    source=source.name,
                )
            )
    if not out:
        raise HarvestError(f"None of {source.name}'s pages could be rendered")
    log.info("Read %d page(s) of %s at %d dpi", len(out), source.name, dpi)
    return out


def read_images(
    paths: list[Path | str], *, width_mm: float, limit: int = 24
) -> list[PageRaster]:
    """Read page images, given how wide the sheet actually is.

    A scan carries no idea of its own size, so the width has to be supplied;
    the height follows from the aspect, and the resolution from both.
    """
    if width_mm <= 0:
        raise HarvestError("The sheet width in millimetres is needed to measure a page image")
    out: list[PageRaster] = []
    for index, item in enumerate(paths[:limit], start=1):
        source = Path(item)
        if not source.exists():
            log.warning("Skipping %s: it does not exist", source)
            continue
        with Image.open(source) as opened:
            image = opened.convert("RGB").copy()
        px_per_mm = image.width / width_mm
        out.append(
            PageRaster(
                index=index,
                image=image,
                width_mm=round(width_mm, 2),
                height_mm=round(image.height / px_per_mm, 2),
                dpi=round(px_per_mm * 25.4, 1),
                source=source.name,
            )
        )
    if not out:
        raise HarvestError("None of those page images could be read")
    return out


def describe(pages: list[PageRaster]) -> dict[str, Any]:
    """A one-line summary of what was read."""
    if not pages:
        return {"pages": 0}
    first = pages[0]
    return {
        "pages": len(pages),
        "width_mm": first.width_mm,
        "height_mm": first.height_mm,
        "dpi": first.dpi,
        "source": first.source,
        "uniform": len({(page.width_mm, page.height_mm) for page in pages}) == 1,
    }
