"""PDF assembly from rendered pages.

The built-in exporter turns page rasters into the preset PDFs of §41. These
tests pin the two properties the rest of the export depends on - every page
present, at the requested resolution - and the memory ceiling that keeps a
full edition from needing a gigabyte of decoded pixels to be saved.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from app.export.exporter import pdf_from_images
from app.vision.renderer import PreviewRenderer

PAGE = (600, 900)
SOURCE_DPI = 150


def _pages(directory, count: int) -> list:
    """Write *count* distinguishable page rasters and return their paths."""
    paths = []
    for index in range(count):
        path = directory / f"page_{index:03d}.png"
        image = Image.new("RGB", PAGE, "white")
        draw = ImageDraw.Draw(image)
        for y in range(0, PAGE[1], 11):
            draw.line([(0, y), (PAGE[0], y)], fill=(index * 9 % 255, 60, 140))
        image.save(path)
        image.close()
        paths.append(path)
    return paths


def test_every_page_is_written_at_the_source_resolution(tmp_path):
    pages = _pages(tmp_path, 5)
    target = pdf_from_images(pages, tmp_path / "out.pdf", SOURCE_DPI, SOURCE_DPI)

    reader = PdfReader(str(target))
    assert len(reader.pages) == 5
    box = reader.pages[0].mediabox
    # 600 px at 150 dpi is four inches, and a PostScript point is 1/72 inch.
    assert float(box.width) == pytest.approx(600 / SOURCE_DPI * 72, abs=0.5)
    assert float(box.height) == pytest.approx(900 / SOURCE_DPI * 72, abs=0.5)


def test_a_lower_target_resolution_keeps_the_physical_page_size(tmp_path):
    pages = _pages(tmp_path, 3)
    full = pdf_from_images(pages, tmp_path / "print.pdf", SOURCE_DPI, SOURCE_DPI)
    web = pdf_from_images(pages, tmp_path / "web.pdf", SOURCE_DPI, 72)

    printed = PdfReader(str(full)).pages[0].mediabox
    reduced = PdfReader(str(web)).pages[0].mediabox
    assert len(PdfReader(str(web)).pages) == 3
    # The digital preset carries fewer pixels but describes the same sheet.
    assert float(reduced.width) == pytest.approx(float(printed.width), abs=1.0)
    assert float(reduced.height) == pytest.approx(float(printed.height), abs=1.0)
    assert web.stat().st_size < full.stat().st_size


def test_only_one_page_is_decoded_at_a_time(tmp_path, monkeypatch):
    """A long edition must not be held in memory to be saved.

    Assembling forty broadsheet pages by handing Pillow every frame at once
    cost 1.2 GB; each page is now decoded, compressed and released in turn, so
    the cost stays that of a single page however long the edition is. The
    check counts how many decoded pages are alive at the same time, which is
    what that memory ceiling actually rests on.
    """
    live: set[int] = set()
    peak = 0
    original_convert = Image.Image.convert
    original_resize = Image.Image.resize
    original_close = Image.Image.close

    def track(image):
        nonlocal peak
        live.add(id(image))
        peak = max(peak, len(live))
        return image

    monkeypatch.setattr(Image.Image, "convert", lambda self, *a, **k: track(original_convert(self, *a, **k)))
    monkeypatch.setattr(Image.Image, "resize", lambda self, *a, **k: track(original_resize(self, *a, **k)))

    def close(self):
        live.discard(id(self))
        original_close(self)

    monkeypatch.setattr(Image.Image, "close", close)

    pages = _pages(tmp_path, 24)
    pdf_from_images(pages, tmp_path / "long.pdf", SOURCE_DPI, SOURCE_DPI)

    assert len(PdfReader(str(tmp_path / "long.pdf")).pages) == 24
    # One decoded page, plus the resized copy while a preset is downsampled.
    assert peak <= 2, f"{peak} decoded pages were alive at once"
    assert not live, "a decoded page was left open"


def test_no_scratch_files_are_left_beside_the_pdf(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    pages = _pages(tmp_path, 2)
    pdf_from_images(pages, output / "edition.pdf", SOURCE_DPI, SOURCE_DPI)

    assert [item.name for item in output.iterdir()] == ["edition.pdf"]


def test_an_empty_page_list_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        pdf_from_images([], tmp_path / "empty.pdf", SOURCE_DPI, SOURCE_DPI)


def test_the_builtin_renderer_writes_a_pdf_page_per_layout_page(template, article_blocks, tmp_path):
    """The fallback PDF must cover the whole edition, not just page one."""
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    blocks = article_blocks
    plan = engine.plan_edition(
        1,
        {1: blocks[:3], 2: blocks[3:]},
        page_count=2,
        publication_name="روزنامه آزمایشی",
        edition_date="۱۴۰۳/۰۵/۰۱",
    )
    renderer = PreviewRenderer(template, dpi=72)

    target = renderer.render_pdf(plan, tmp_path / "fallback.pdf")

    assert len(PdfReader(str(target)).pages) == len(plan.pages)
    # The rasters go to a scratch directory of their own, not next to the PDF.
    assert [item.name for item in tmp_path.iterdir()] == ["fallback.pdf"]


def test_two_exports_at_once_do_not_interleave(tmp_path):
    """The service is shared; the UI can start an export while one is running.

    Per-run state used to live on the instance, so a second export could read
    the first one's rendered pages. The lock is what stops that, and this
    pins it.
    """
    import threading
    import time

    from app.export.exporter import ExportService

    service = ExportService()
    order: list[str] = []
    entered = threading.Event()

    def slow_export(name: str) -> None:
        with service._lock:
            order.append(f"{name}-in")
            entered.set()
            time.sleep(0.15)
            order.append(f"{name}-out")

    first = threading.Thread(target=slow_export, args=("a",))
    second = threading.Thread(target=slow_export, args=("b",))
    first.start()
    entered.wait(2)
    second.start()
    first.join(5)
    second.join(5)

    # Whichever ran first, it finished before the other started.
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])


def test_the_export_service_keeps_no_per_run_state(tmp_path):
    """Nothing an export produces may be stashed on the shared instance."""
    from app.export.exporter import ExportService

    service = ExportService()
    stateful = [
        name
        for name, value in vars(service).items()
        if isinstance(value, list | dict) and not name.startswith("_")
    ]
    assert stateful == [], f"per-run state on a shared service: {stateful}"
