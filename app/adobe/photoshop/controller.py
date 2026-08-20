"""Photoshop controller.

Each operation is attempted through Photoshop's scripting API first. When
Photoshop is absent, or when a specific feature is missing from the installed
version (background removal on pre-2020 builds, for example), the controller
performs the same operation locally with Pillow and records which path was
used. The result is the same file on disk either way, so the layout stage
never has to care.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from app.adobe.bridge import AdobeBridge, ScriptResult, working_directory
from app.adobe.detect import AdobeApp, detect_photoshop
from app.adobe.jsx import ScriptBuilder, build_photoshop_script, build_probe_script
from app.core.errors import AssetError
from app.core.events import EventBus, EventType
from app.utils import imaging

log = logging.getLogger(__name__)

Mode = Literal["rgb", "cmyk", "gray"]


class PhotoshopController:
    """Drives Adobe Photoshop, with a local Pillow implementation as backup."""

    def __init__(
        self,
        work_dir: Path,
        *,
        app: AdobeApp | None = None,
        bus: EventBus | None = None,
        prefer_com: bool = True,
        allow_queue: bool = True,
        script_timeout: float = 300.0,
        launch_timeout: float = 180.0,
        allow_local_fallback: bool = True,
    ) -> None:
        self.app = app or detect_photoshop()
        self.work_dir = working_directory(Path(work_dir), "photoshop")
        self.bridge = AdobeBridge(
            self.app,
            self.work_dir,
            prefer_com=prefer_com,
            allow_queue=allow_queue,
            default_timeout=script_timeout,
        )
        self.bus = bus
        self.launch_timeout = launch_timeout
        self.allow_local_fallback = allow_local_fallback
        self._connected = False
        self._capabilities: dict[str, Any] | None = None

    # ---------------------------------------------------------- life-cycle
    def available(self) -> bool:
        """Whether Photoshop is installed."""
        return self.app.installed

    def connect(self, launch: bool = True) -> str:
        """Connect to Photoshop, launching it when necessary."""
        strategy = self.bridge.connect(launch=launch, timeout=self.launch_timeout)
        self._connected = True
        self._emit(EventType.ADOBE_CONNECTED, host="photoshop", strategy=strategy.name)
        return strategy.name

    def capabilities(self) -> dict[str, Any]:
        """Feature probe of the installed Photoshop version (cached)."""
        if self._capabilities is not None:
            return self._capabilities
        if not self.app.installed:
            self._capabilities = {"installed": False, "local_fallback": self.allow_local_fallback}
            return self._capabilities
        try:
            self.connect()
            result = self.bridge.run(build_probe_script("photoshop"), operation="probe")
            data = dict(result.data or {})
            self._capabilities = {"installed": True, **data}
        except Exception as exc:  # noqa: BLE001 - probing must not raise
            log.warning("Photoshop probe failed: %s", exc)
            self._capabilities = {
                "installed": True,
                "reachable": False,
                "error": str(exc)[:300],
                "local_fallback": self.allow_local_fallback,
            }
        return self._capabilities

    def health(self) -> dict[str, Any]:
        """Diagnostics entry point."""
        return {**self.app.to_dict(), **self.capabilities()}

    def disconnect(self) -> None:
        """Release the connection."""
        self.bridge.shutdown()
        self._connected = False
        self._emit(EventType.ADOBE_DISCONNECTED, host="photoshop")

    shutdown = disconnect

    # ------------------------------------------------------------ pipeline
    def process_image(
        self,
        source: Path | str,
        target: Path | str,
        *,
        aspect: float | None = None,
        width_px: int | None = None,
        height_px: int | None = None,
        dpi: int = 300,
        mode: Mode | None = None,
        remove_background: bool = False,
        adjust: dict[str, Any] | None = None,
        quality: int = 92,
        focus_y: float = 0.42,
    ) -> dict[str, Any]:
        """Run the full "prepare this photo for print" pass.

        Returns a dictionary describing the produced file, including which
        engine did the work (``"photoshop"`` or ``"pillow"``).
        """
        source, target = Path(source), Path(target)
        if not source.exists():
            raise AssetError(f"Source image does not exist: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)

        spec: dict[str, Any] = {
            "source": str(source),
            "target": str(target),
            "dpi": dpi,
            "quality": quality,
            "focus_y": focus_y,
            "flatten": True,
        }
        if aspect:
            spec["aspect"] = round(aspect, 5)
        if width_px:
            spec["width_px"] = int(width_px)
        if height_px:
            spec["height_px"] = int(height_px)
        if mode:
            spec["mode"] = mode
        if remove_background:
            spec["remove_background"] = True
        if adjust:
            spec["adjust"] = adjust

        if self.app.installed:
            try:
                self.connect()
                result = self.bridge.run(build_photoshop_script(spec), operation="process_image")
                if result.ok:
                    data = dict(result.data or {})
                    data.update({"engine": "photoshop", "strategy": result.strategy, "path": str(target)})
                    self._emit(
                        EventType.ADOBE_COMMAND, host="photoshop", command="process_image", path=str(target)
                    )
                    return data
                log.warning("Photoshop reported an error: %s", result.message())
            except Exception as exc:  # noqa: BLE001 - fall through to Pillow
                log.warning("Photoshop processing failed (%s); using the local engine", exc)

        if not self.allow_local_fallback:
            raise AssetError(f"Photoshop could not process {source.name} and the local engine is disabled")
        return self._process_locally(
            source,
            target,
            aspect=aspect,
            width_px=width_px,
            height_px=height_px,
            adjust=adjust,
            quality=quality,
            mode=mode,
            remove_background=remove_background,
        )

    def _process_locally(
        self,
        source: Path,
        target: Path,
        *,
        aspect: float | None,
        width_px: int | None,
        height_px: int | None,
        adjust: dict[str, Any] | None,
        quality: int,
        mode: Mode | None,
        remove_background: bool,
    ) -> dict[str, Any]:
        """Perform the same operations with Pillow."""
        notes: list[str] = []
        current = source
        if aspect:
            cropped = target.with_name(f"{target.stem}_crop{target.suffix}")
            current = imaging.smart_crop(current, cropped, aspect, quality=quality)
        if width_px or height_px:
            analysis = imaging.image_info(current)
            final_w = width_px or int(analysis["width"])
            final_h = height_px or int(
                round(final_w / max(1e-6, aspect or (analysis["width"] / max(1, analysis["height"]))))
            )
            resized = target.with_name(f"{target.stem}_resize{target.suffix}")
            current = imaging.resize_to_fit(current, resized, final_w, final_h, quality=quality)
        if adjust:
            enhanced = target.with_name(f"{target.stem}_adjust{target.suffix}")
            current = imaging.enhance(
                current,
                enhanced,
                brightness=1.0 + (adjust.get("brightness", 0) or 0) / 150.0,
                contrast=1.0 + (adjust.get("contrast", 0) or 0) / 100.0,
                saturation=1.0 + (adjust.get("saturation", 0) or 0) / 100.0,
                sharpen=1.0 + ((adjust.get("sharpen") or {}).get("amount", 0) or 0) / 200.0,
                quality=quality,
            )
        else:
            corrected = target.with_name(f"{target.stem}_auto{target.suffix}")
            current = imaging.auto_correct(current, corrected)
        if mode == "gray":
            grey = target.with_name(f"{target.stem}_gray{target.suffix}")
            current = imaging.to_grayscale(current, grey, quality=quality)
        elif mode == "cmyk":
            notes.append("CMYK conversion requires Photoshop; the file stays in RGB.")
        if remove_background:
            notes.append("Background removal requires Photoshop; the original background is kept.")

        if Path(current) != target:
            with imaging.open_image(current) as image:
                imaging._save(image, target, quality)  # noqa: SLF001 - shared writer
        for temporary in target.parent.glob(f"{target.stem}_*{target.suffix}"):
            if temporary != target:
                temporary.unlink(missing_ok=True)

        info = imaging.image_info(target)
        if notes:
            log.info("Local image processing notes: %s", "; ".join(notes))
        return {
            "engine": "pillow",
            "path": str(target),
            "exists": target.exists(),
            "width": info["width"],
            "height": info["height"],
            "resolution": info["dpi"],
            "notes": notes,
        }

    # --------------------------------------------------- single operations
    def open_image(self, path: Path | str) -> dict[str, Any]:
        """Open an image in Photoshop."""
        builder = ScriptBuilder("photoshop", "open_image")
        builder.call("setup")
        builder.call("openImage", str(path), assign="__i")
        builder.emit("__i")
        return dict(self._run(builder.build(), "open_image").data or {})

    def create_document(self, width: int, height: int, dpi: int = 300, mode: Mode = "rgb") -> dict[str, Any]:
        """Create a new Photoshop document."""
        builder = ScriptBuilder("photoshop", "create_document")
        builder.call("setup")
        builder.call(
            "createDocument", {"width": width, "height": height, "dpi": dpi, "mode": mode}, assign="__i"
        )
        builder.emit("__i")
        return dict(self._run(builder.build(), "create_document").data or {})

    def resize(self, width_px: int | None, height_px: int | None, dpi: int = 300) -> dict[str, Any]:
        """Resample the open document."""
        builder = ScriptBuilder("photoshop", "resize")
        builder.call("resize", width_px, height_px, dpi, assign="__i")
        builder.emit("__i")
        return dict(self._run(builder.build(), "resize").data or {})

    def crop(self, left: float, top: float, right: float, bottom: float) -> dict[str, Any]:
        """Crop the open document to a pixel box."""
        builder = ScriptBuilder("photoshop", "crop")
        builder.call("crop", left, top, right, bottom, assign="__i")
        builder.emit("__i")
        return dict(self._run(builder.build(), "crop").data or {})

    def crop_to_aspect(self, aspect: float, focus_y: float = 0.42) -> dict[str, Any]:
        """Crop the open document to an aspect ratio."""
        builder = ScriptBuilder("photoshop", "crop_to_aspect")
        builder.call("cropToAspect", aspect, focus_y, assign="__i")
        builder.emit("__i")
        return dict(self._run(builder.build(), "crop_to_aspect").data or {})

    def remove_background(self) -> dict[str, Any]:
        """Remove the background of the open document."""
        builder = ScriptBuilder("photoshop", "remove_background")
        builder.call("removeBackground", assign="__r")
        builder.emit("__r")
        return dict(self._run(builder.build(), "remove_background").data or {})

    def adjust(self, **options: Any) -> dict[str, Any]:
        """Apply tonal corrections to the open document."""
        builder = ScriptBuilder("photoshop", "adjust")
        builder.call("adjust", options, assign="__i")
        builder.emit("__i")
        return dict(self._run(builder.build(), "adjust").data or {})

    def convert_mode(self, mode: Mode) -> dict[str, Any]:
        """Convert the colour mode of the open document."""
        builder = ScriptBuilder("photoshop", "convert_mode")
        builder.call("convertMode", mode, assign="__i")
        builder.emit("__i")
        return dict(self._run(builder.build(), "convert_mode").data or {})

    def to_smart_object(self) -> bool:
        """Convert the active layer into a smart object."""
        builder = ScriptBuilder("photoshop", "to_smart_object")
        builder.call("toSmartObject", assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "to_smart_object").data)

    def export(self, target: Path | str, quality: int = 92) -> Path:
        """Export the open document; the format follows the suffix."""
        target = Path(target)
        suffix = target.suffix.lower()
        method = {
            ".png": "exportPNG",
            ".tif": "exportTIFF",
            ".tiff": "exportTIFF",
            ".psd": "savePSD",
        }.get(suffix, "exportJPEG")
        builder = ScriptBuilder("photoshop", "export")
        if method == "exportJPEG":
            builder.call(method, str(target), quality, assign="__r")
        else:
            builder.call(method, str(target), assign="__r")
        builder.emit("__r")
        self._run(builder.build(), "export")
        return target

    def close_document(self, save: bool = False) -> bool:
        """Close the open document."""
        builder = ScriptBuilder("photoshop", "close_document")
        builder.call("closeDocument", save, assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "close_document").data)

    def close_all(self) -> bool:
        """Close every open document."""
        builder = ScriptBuilder("photoshop", "close_all")
        builder.call("closeAll", assign="__ok")
        builder.emit("__ok")
        return bool(self._run(builder.build(), "close_all").data)

    # ------------------------------------------------------------ internals
    def _run(self, script: Any, operation: str) -> ScriptResult:
        if not self._connected:
            self.connect()
        return self.bridge.run(script, operation=operation).raise_for_status(operation)

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)

    def describe(self) -> dict[str, Any]:
        """Diagnostics summary."""
        return {**self.bridge.describe(), "local_fallback": self.allow_local_fallback}
