"""Image intelligence and processing helpers (Pillow based).

Everything the layout engine needs to know about a picture *before* it is
placed - real resolution, sharpness, exposure, colourfulness, faces and
duplicates - is computed here. Face detection uses OpenCV's Haar cascades
when OpenCV is installed and degrades to a luminance/skin-tone region
detector otherwise; the resulting count is only ever used as a hint for
cropping and for preferring portraits in personality stories.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

from app.models.schemas import ImageAnalysis
from app.utils.files import checksum
from app.utils.units import effective_dpi

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 400_000_000

SUPPORTED_INPUT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".psd"}

try:  # pragma: no cover - optional dependency
    import cv2  # type: ignore
    import numpy as _np  # type: ignore

    _CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _np = None  # type: ignore
    _CV2 = False


def open_image(path: Path | str) -> Image.Image:
    """Open an image, applying EXIF orientation and converting to RGB."""
    image = Image.open(path)
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:  # pragma: no cover - broken EXIF
        pass
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def _sharpness(image: Image.Image) -> float:
    """Edge-energy based sharpness score (variance of the edge response)."""
    grey = image.convert("L")
    grey.thumbnail((640, 640))
    edges = grey.filter(ImageFilter.Kernel((3, 3), [0, -1, 0, -1, 4, -1, 0, -1, 0], scale=1, offset=128))
    stat = ImageStat.Stat(edges)
    return round(float(stat.stddev[0]) ** 2 / 10.0, 3)


def _colorfulness(image: Image.Image) -> float:
    """Colourfulness metric (Hasler-Susstrunk, absolute-difference variant).

    Scaled to roughly ``0..100``; used to prefer vivid pictures for lead slots
    and to warn about washed-out photographs.
    """
    if image.mode != "RGB":
        return 0.0
    from PIL import ImageChops

    small = image.copy()
    small.thumbnail((320, 320))
    red, green, blue = small.split()[:3]
    rg = ImageChops.difference(red, green)
    yellow = ImageChops.add(red, green, scale=2.0)
    yb = ImageChops.difference(yellow, blue)
    rg_stat, yb_stat = ImageStat.Stat(rg), ImageStat.Stat(yb)
    std = math.sqrt(rg_stat.stddev[0] ** 2 + yb_stat.stddev[0] ** 2)
    mean = math.sqrt(rg_stat.mean[0] ** 2 + yb_stat.mean[0] ** 2)
    return round(min(100.0, std + 0.3 * mean), 2)


def _count_faces(image: Image.Image) -> int:
    """Number of faces detected; ``0`` when no detector is usable."""
    if _CV2:  # pragma: no cover - requires opencv
        try:
            small = image.convert("RGB").copy()
            small.thumbnail((900, 900))
            array = _np.array(small)[:, :, ::-1]
            grey = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(str(cascade_path))
            if cascade.empty():
                return 0
            faces = cascade.detectMultiScale(grey, scaleFactor=1.15, minNeighbors=5, minSize=(36, 36))
            return int(len(faces))
        except Exception as exc:
            log.debug("OpenCV face detection failed: %s", exc)
    return _skin_region_estimate(image)


def _skin_region_estimate(image: Image.Image) -> int:
    """Coarse fallback: count connected skin-tone blobs on a downscaled image.

    This is a hint, not a detector; it exists so that portrait-heavy pictures
    are still preferred for personality stories on machines without OpenCV.
    """
    if image.mode != "RGB":
        return 0
    small = image.copy()
    small.thumbnail((96, 96))
    width, height = small.size
    pixels = small.load()
    mask = [[False] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y][:3]
            if r > 95 and g > 40 and b > 20 and r > g > b and (r - min(g, b)) > 15 and abs(r - g) > 10:
                mask[y][x] = True
    seen = [[False] * width for _ in range(height)]
    blobs = 0
    min_area = max(30, (width * height) // 120)
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < height and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            if area >= min_area:
                blobs += 1
    return min(blobs, 12)


def perceptual_hash(path: Path | str, size: int = 8) -> str:
    """Average-hash of an image, used to spot duplicates and near-duplicates."""
    with open_image(path) as image:
        grey = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(grey.getdata())
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return f"{int(bits, 2):0{size * size // 4}x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Bit difference between two perceptual hashes."""
    if len(hash_a) != len(hash_b):
        return 64
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def analyze(path: Path | str, *, target_width_mm: float | None = None) -> ImageAnalysis:
    """Full image intelligence pass (see specification §39)."""
    file = Path(path)
    result = ImageAnalysis(path=str(file))
    try:
        with open_image(file) as image:
            result.width, result.height = image.size
            result.aspect_ratio = round(image.width / image.height, 4) if image.height else 1.0
            dpi = image.info.get("dpi")
            result.dpi = float(dpi[0]) if dpi and dpi[0] else 72.0
            result.orientation = (
                "square"
                if abs(image.width - image.height) <= 0.02 * max(image.size)
                else ("landscape" if image.width > image.height else "portrait")
            )
            grey = image.convert("L")
            stat = ImageStat.Stat(grey)
            result.brightness = round(stat.mean[0] / 255.0 * 100.0, 2)
            result.contrast = round(stat.stddev[0] / 128.0 * 100.0, 2)
            result.sharpness = _sharpness(image)
            result.colorfulness = _colorfulness(image)
            result.face_count = _count_faces(image)
        result.checksum = checksum(file)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot analyse image %s: %s", file, exc)
        result.problems.append(f"unreadable: {exc}")
        return result

    if result.width < 800 or result.height < 600:
        result.problems.append("low_resolution")
    if result.sharpness < 8.0:
        result.problems.append("blurry")
    if result.brightness < 12.0:
        result.problems.append("underexposed")
    elif result.brightness > 92.0:
        result.problems.append("overexposed")
    if result.contrast < 8.0:
        result.problems.append("flat_contrast")
    if target_width_mm:
        printed_dpi = effective_dpi(result.width, target_width_mm)
        if printed_dpi < 200:
            result.problems.append(f"print_dpi_{printed_dpi:.0f}")

    result.quality_score = round(quality_score(result), 2)
    return result


def quality_score(analysis: ImageAnalysis) -> float:
    """Composite 0..100 usability score for an analysed image."""
    pixels = analysis.width * analysis.height
    resolution = min(1.0, pixels / (1600 * 1067)) * 35.0
    sharpness = min(1.0, analysis.sharpness / 45.0) * 30.0
    exposure = max(0.0, 1.0 - abs(analysis.brightness - 52.0) / 52.0) * 15.0
    contrast = min(1.0, analysis.contrast / 28.0) * 12.0
    color = min(1.0, analysis.colorfulness / 45.0) * 8.0
    score = resolution + sharpness + exposure + contrast + color
    for problem in analysis.problems:
        if problem.startswith("print_dpi_"):
            score -= 12.0
        elif problem in ("blurry", "low_resolution"):
            score -= 15.0
        else:
            score -= 6.0
    return max(0.0, min(100.0, score))


def entropy_crop_box(image: Image.Image, aspect: float) -> tuple[int, int, int, int]:
    """Best crop box for *aspect* chosen by maximising local detail.

    A sliding window over the image scores candidate crops by the standard
    deviation of their edge response, which keeps faces and text-free detail
    inside the frame instead of blindly centre-cropping.
    """
    width, height = image.size
    current = width / height
    if abs(current - aspect) < 0.01:
        return (0, 0, width, height)
    if current > aspect:
        crop_w, crop_h = int(height * aspect), height
    else:
        crop_w, crop_h = width, int(width / aspect)
    crop_w, crop_h = max(1, min(crop_w, width)), max(1, min(crop_h, height))

    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    steps = 12
    best_box = (0, 0, crop_w, crop_h)
    best_score = -1.0
    max_dx, max_dy = width - crop_w, height - crop_h
    for i in range(steps + 1):
        dx = int(max_dx * i / steps) if max_dx else 0
        dy = int(max_dy * i / steps) if max_dy else 0
        box = (dx, dy, dx + crop_w, dy + crop_h)
        window = edges.crop(box)
        window.thumbnail((160, 160))
        score = ImageStat.Stat(window).stddev[0]
        # Bias slightly towards the upper third, where subjects usually sit.
        score *= 1.0 + 0.12 * (1.0 - abs((dy + crop_h / 2) / height - 0.42))
        if score > best_score:
            best_score, best_box = score, box
        if not max_dx and not max_dy:
            break
    return best_box


def smart_crop(
    source: Path | str,
    target: Path | str,
    aspect: float,
    *,
    max_width: int | None = None,
    quality: int = 92,
) -> Path:
    """Crop *source* to *aspect* using :func:`entropy_crop_box` and save it."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open_image(source) as image:
        cropped = image.crop(entropy_crop_box(image, aspect))
        if max_width and cropped.width > max_width:
            ratio = max_width / cropped.width
            cropped = cropped.resize(
                (max_width, max(1, int(cropped.height * ratio))), Image.Resampling.LANCZOS
            )
        _save(cropped, destination, quality)
    return destination


def resize_to_fit(
    source: Path | str, target: Path | str, width_px: int, height_px: int, *, quality: int = 92
) -> Path:
    """Resize *source* so it covers ``width_px x height_px`` and centre-crop."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open_image(source) as image:
        fitted = ImageOps.fit(
            image, (max(1, width_px), max(1, height_px)), Image.Resampling.LANCZOS, centering=(0.5, 0.42)
        )
        _save(fitted, destination, quality)
    return destination


def enhance(
    source: Path | str,
    target: Path | str,
    *,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    sharpen: float = 1.0,
    quality: int = 92,
) -> Path:
    """Apply basic tonal corrections (the Pillow path of the Photoshop layer)."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open_image(source) as image:
        result = image
        if brightness != 1.0:
            result = ImageEnhance.Brightness(result).enhance(brightness)
        if contrast != 1.0:
            result = ImageEnhance.Contrast(result).enhance(contrast)
        if saturation != 1.0 and result.mode == "RGB":
            result = ImageEnhance.Color(result).enhance(saturation)
        if sharpen != 1.0:
            result = ImageEnhance.Sharpness(result).enhance(sharpen)
        _save(result, destination, quality)
    return destination


def auto_correct(source: Path | str, target: Path | str, analysis: ImageAnalysis | None = None) -> Path:
    """Derive and apply a correction based on the measured exposure/contrast."""
    info = analysis or analyze(source)
    brightness = 1.0
    if info.brightness < 40:
        brightness = min(1.45, 52.0 / max(12.0, info.brightness))
    elif info.brightness > 68:
        brightness = max(0.75, 58.0 / info.brightness)
    contrast = 1.0
    if info.contrast < 18:
        contrast = min(1.35, 22.0 / max(6.0, info.contrast))
    sharpen = 1.25 if info.sharpness < 20 else 1.0
    return enhance(source, target, brightness=brightness, contrast=contrast, sharpen=sharpen)


def to_grayscale(source: Path | str, target: Path | str, quality: int = 92) -> Path:
    """Convert to greyscale (mono print sections)."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open_image(source) as image:
        _save(image.convert("L"), destination, quality)
    return destination


def make_preview(source: Path | str, target: Path | str, max_side: int = 480) -> Path:
    """Write a small JPEG/PNG preview used by the asset browser."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open_image(source) as image:
        preview = image.copy()
        preview.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        _save(preview, destination, 85)
    return destination


def placeholder(
    target: Path | str, width: int, height: int, *, label: str = "", color: str = "#d8d8d8"
) -> Path:
    """Create a neutral grey frame filler.

    Used only when an image slot must exist in the InDesign document but no
    usable picture is available; the frame is flagged in the QA report so the
    operator can replace it.
    """
    from PIL import ImageDraw

    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (max(2, width), max(2, height)), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, image.width - 1, image.height - 1], outline="#9a9a9a", width=3)
    draw.line([0, 0, image.width, image.height], fill="#bcbcbc", width=2)
    draw.line([0, image.height, image.width, 0], fill="#bcbcbc", width=2)
    if label:
        draw.text((12, 12), label[:60], fill="#5a5a5a")
    image.save(destination)
    return destination


def _save(image: Image.Image, destination: Path, quality: int) -> None:
    suffix = destination.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        image.convert("RGB").save(destination, "JPEG", quality=quality, optimize=True, dpi=(300, 300))
    elif suffix == ".png":
        image.save(destination, "PNG", optimize=True)
    elif suffix in (".tif", ".tiff"):
        image.save(destination, "TIFF", dpi=(300, 300))
    else:
        image.save(destination)


def image_info(path: Path | str) -> dict[str, Any]:
    """Lightweight metadata read without a full analysis pass."""
    try:
        with Image.open(path) as image:
            dpi = image.info.get("dpi") or (72, 72)
            return {
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "format": image.format,
                "dpi": float(dpi[0] or 72),
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("image_info failed for %s: %s", path, exc)
        return {"width": 0, "height": 0, "mode": "", "format": "", "dpi": 72.0}
