"""Did the design come out the way it was asked for?

The quality check elsewhere asks whether a design is *good*. This asks a
narrower and more important question: whether it is what the operator
specified. A font that is not installed, a colour the mode cannot hold, a
size the application rounded, a layer that failed silently - each of them
produces something plausible that is not what was requested, and none of them
announces itself.

So every instruction that can be measured is compared against what the host
reported building, and anything the host could not honour is named rather
than left for the operator to notice on the printed sheet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.design.plan import Box, DesignPlan, Layer

log = logging.getLogger(__name__)

#: How far a value may drift and still count as honoured.
POSITION_TOLERANCE_PX = 2.0
SIZE_TOLERANCE_PT = 0.25
#: Two colours closer than this are the same to the eye (sRGB, 0-255).
COLOR_TOLERANCE = 6
#: A box on a print canvas that would make sense in millimetres, and does not
#: in pixels, is almost always a millimetre typed where a pixel was meant.
#: The tell is a measurable contradiction rather than a size threshold.
UNIT_MISTAKE_HEADROOM = 0.05


class Severity(str, Enum):
    """How much a difference matters."""

    BLOCKING = "blocking"
    """The result is not what was asked for and cannot be passed off as it."""
    NOTABLE = "notable"
    """A real difference the operator has to know about."""
    MINOR = "minor"
    """Within what the application rounds to."""


@dataclass
class Difference:
    """One thing that is not as it was specified."""

    layer: str
    attribute: str
    asked: Any
    produced: Any
    severity: Severity = Severity.NOTABLE
    message: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "layer": self.layer,
            "attribute": self.attribute,
            "asked": self.asked,
            "produced": self.produced,
            "severity": self.severity.value,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class FidelityReport:
    """Everything the host could not honour."""

    differences: list[Difference] = field(default_factory=list)
    checked: int = 0
    host: str = ""

    @property
    def faithful(self) -> bool:
        """Whether nothing blocking or notable differs."""
        return not any(d.severity is not Severity.MINOR for d in self.differences)

    def blocking(self) -> list[Difference]:
        """Only the differences that cannot be passed off."""
        return [d for d in self.differences if d.severity is Severity.BLOCKING]

    def summary(self) -> str:
        """One line for the run log."""
        if self.faithful:
            return f"{self.checked} instruction(s) checked; all honoured"
        counts: dict[str, int] = {}
        for difference in self.differences:
            counts[difference.severity.value] = counts.get(difference.severity.value, 0) + 1
        parts = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        return f"{self.checked} instruction(s) checked; {parts}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "host": self.host,
            "checked": self.checked,
            "faithful": self.faithful,
            "summary": self.summary(),
            "differences": [d.to_dict() for d in self.differences],
        }


def parse_color(value: str | None) -> tuple[int, int, int] | None:
    """``#rgb``/``#rrggbb`` to an RGB triple, or ``None``."""
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) < 6:
        return None
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def color_distance(left: str | None, right: str | None) -> int | None:
    """Largest per-channel difference between two colours."""
    a, b = parse_color(left), parse_color(right)
    if a is None or b is None:
        return None
    return max(abs(a[i] - b[i]) for i in range(3))


class DesignFidelity:
    """Compares a design plan against what a host reported building."""

    def __init__(self, plan: DesignPlan) -> None:
        self.plan = plan

    # ------------------------------------------------------------- checking
    def check(self, report: dict[str, Any], *, host: str = "photoshop") -> FidelityReport:
        """Compare *report* - the host's own account - against the plan."""
        out = FidelityReport(host=host)
        produced = {str(entry.get("name")): entry for entry in (report.get("layers") or [])}

        self._check_canvas(report, out)
        self._check_substitutions(report, out)

        for layer in self.plan.ordered():
            if layer.kind.value == "group":
                continue
            out.checked += 1
            entry = produced.get(layer.name)
            if entry is None:
                out.differences.append(
                    Difference(
                        layer=layer.name,
                        attribute="exists",
                        asked="present",
                        produced="missing",
                        severity=Severity.BLOCKING,
                        message=f"'{layer.name}' is not in the finished design",
                        suggestion="Check the run log for the layer that failed to build.",
                    )
                )
                continue
            self._check_geometry(layer, entry, out)
            if layer.is_text:
                self._check_type(layer, entry, out)
            self._check_appearance(layer, entry, out)
        log.info("Fidelity (%s): %s", host, out.summary())
        return out

    def check_plan(self) -> FidelityReport:
        """Check what can be checked before anything is built.

        Catches the mistakes that are cheapest to catch early: a box given in
        the wrong unit, a layer outside the canvas, type that cannot fit.
        """
        out = FidelityReport(host="plan")
        canvas = self.plan.canvas.box
        physical = self.plan.canvas.medium == "print"
        for layer in self.plan.ordered():
            if layer.kind.value == "group":
                continue
            out.checked += 1
            if layer.box.area <= 0:
                out.differences.append(
                    Difference(
                        layer=layer.name,
                        attribute="box",
                        asked=layer.box.model_dump(),
                        produced="no area",
                        severity=Severity.BLOCKING,
                        message=f"'{layer.name}' has no width or height",
                    )
                )
                continue
            if not canvas.contains(layer.box, tolerance=1.0) and layer.box.overlaps(canvas) <= 0:
                out.differences.append(
                    Difference(
                        layer=layer.name,
                        attribute="box",
                        asked=layer.box.model_dump(),
                        produced="off the canvas",
                        severity=Severity.BLOCKING,
                        message=f"'{layer.name}' sits entirely outside the canvas",
                        suggestion="Boxes are in pixels; use plan.mm(...) for millimetres.",
                    )
                )
                continue
            if physical and self._looks_like_a_unit_mistake(layer, canvas):
                out.differences.append(
                    Difference(
                        layer=layer.name,
                        attribute="box",
                        asked=layer.box.model_dump(),
                        produced=layer.box.to_mm(self.plan.canvas.dpi),
                        severity=Severity.NOTABLE,
                        message=(
                            f"'{layer.name}' covers {layer.box.width:.0f}x{layer.box.height:.0f} px "
                            f"of a {canvas.width:.0f}x{canvas.height:.0f} px canvas, which is the "
                            "size millimetres would give"
                        ),
                        suggestion="Use plan.mm(x, y, width, height) if those were millimetres.",
                    )
                )
            if layer.kind.value == "text" and layer.text.strip():
                self._check_fit(layer, out)
            safe = self.plan.canvas.safe_box
            if safe is not None and layer.role in ("headline", "kicker", "body", "cta"):
                if not safe.contains(layer.box, tolerance=2.0):
                    out.differences.append(
                        Difference(
                            layer=layer.name,
                            attribute="safe_area",
                            asked="inside the safe area",
                            produced="outside it",
                            severity=Severity.NOTABLE,
                            message=(f"'{layer.name}' reaches outside the area this platform leaves clear"),
                            suggestion="The caption and buttons sit over it; move the text inward.",
                        )
                    )
        return out

    def _check_fit(self, layer: Layer, out: FidelityReport) -> None:
        """Whether the copy actually fits the box it was given.

        Type running off the sheet is the most visible way a design fails, and
        it is entirely measurable before anything is built.
        """
        from app.design.renderer import measure_text

        try:
            measured = measure_text(self.plan, layer)
        except Exception as exc:  # noqa: BLE001 - a missing font is not a fidelity fault
            log.debug("'%s' could not be measured: %s", layer.name, exc)
            return
        overflow = float(measured["overflow"])
        if overflow <= 0.0:
            return
        out.differences.append(
            Difference(
                layer=layer.name,
                attribute="fit",
                asked=f"{layer.box.height:.0f} px of box",
                produced=f"{measured['measured_height']:.0f} px of copy",
                severity=Severity.BLOCKING if overflow > 0.08 else Severity.NOTABLE,
                message=(
                    f"'{layer.name}' overruns its box by {overflow * 100:.0f}% at "
                    f"{layer.size_pt:g} pt ({measured['lines']} line(s))"
                ),
                suggestion="Set the size smaller, make the box taller, or cut the copy.",
            )
        )

    # -------------------------------------------------------------- details
    def _check_canvas(self, report: dict[str, Any], out: FidelityReport) -> None:
        canvas = self.plan.canvas
        for attribute, asked, produced in (
            ("width", canvas.width_px, report.get("width")),
            ("height", canvas.height_px, report.get("height")),
            ("dpi", canvas.dpi, report.get("resolution")),
        ):
            if produced is None:
                continue
            if abs(float(produced) - float(asked)) > 1:
                out.differences.append(
                    Difference(
                        layer="canvas",
                        attribute=attribute,
                        asked=asked,
                        produced=produced,
                        severity=Severity.BLOCKING,
                        message=f"The canvas is {produced} rather than {asked}",
                    )
                )
        mode = report.get("mode")
        if mode and canvas.mode not in str(mode).lower():
            out.differences.append(
                Difference(
                    layer="canvas",
                    attribute="mode",
                    asked=canvas.mode,
                    produced=str(mode),
                    severity=Severity.NOTABLE,
                    message=f"The document is in {mode}, not {canvas.mode.upper()}",
                    suggestion="A print piece delivered in RGB will shift on press.",
                )
            )

    def _check_substitutions(self, report: dict[str, Any], out: FidelityReport) -> None:
        for entry in report.get("font_substitutions") or []:
            asked = entry.get("asked")
            got = entry.get("got")
            out.differences.append(
                Difference(
                    layer="fonts",
                    attribute="font",
                    asked=asked,
                    produced=got or "the application default",
                    severity=Severity.BLOCKING,
                    message=(
                        f"'{asked}' is not installed; "
                        f"{'the design used ' + str(got) if got else 'the default face was used'}"
                    ),
                    suggestion=f"Install {asked}, or choose a face that is available.",
                )
            )

    def _check_geometry(self, layer: Layer, entry: dict[str, Any], out: FidelityReport) -> None:
        if entry.get("x") is None:
            return
        produced = Box(
            x=float(entry.get("x", 0)),
            y=float(entry.get("y", 0)),
            width=float(entry.get("width", 0)),
            height=float(entry.get("height", 0)),
        )
        # A text layer's own bounds follow its glyphs, not its frame, so only
        # a frame that moved is worth reporting.
        wanted = layer.box
        drift = max(abs(produced.x - wanted.x), abs(produced.y - wanted.y))
        if layer.is_text:
            if drift > POSITION_TOLERANCE_PX * 6:
                out.differences.append(
                    Difference(
                        layer=layer.name,
                        attribute="position",
                        asked={"x": wanted.x, "y": wanted.y},
                        produced={"x": produced.x, "y": produced.y},
                        severity=Severity.NOTABLE,
                        message=f"'{layer.name}' sits {drift:.0f} px from where it was placed",
                    )
                )
            return
        if drift > POSITION_TOLERANCE_PX:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="position",
                    asked={"x": wanted.x, "y": wanted.y},
                    produced={"x": produced.x, "y": produced.y},
                    severity=Severity.MINOR if drift < 6 else Severity.NOTABLE,
                    message=f"'{layer.name}' is {drift:.1f} px from where it was placed",
                )
            )
        size_drift = max(abs(produced.width - wanted.width), abs(produced.height - wanted.height))
        if wanted.area > 0 and size_drift > max(POSITION_TOLERANCE_PX, wanted.width * 0.01):
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="size",
                    asked={"width": wanted.width, "height": wanted.height},
                    produced={"width": produced.width, "height": produced.height},
                    severity=Severity.NOTABLE,
                    message=f"'{layer.name}' came out {size_drift:.0f} px from the size asked for",
                )
            )

    def _check_type(self, layer: Layer, entry: dict[str, Any], out: FidelityReport) -> None:
        produced_text = entry.get("text")
        if produced_text is not None and str(produced_text) != layer.text:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="text",
                    asked=layer.text[:120],
                    produced=str(produced_text)[:120],
                    severity=Severity.BLOCKING,
                    message=f"'{layer.name}' does not carry the words it was given",
                )
            )
        size = entry.get("size_pt")
        if size is not None and layer.size_pt and abs(float(size) - layer.size_pt) > SIZE_TOLERANCE_PT:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="size_pt",
                    asked=layer.size_pt,
                    produced=float(size),
                    severity=Severity.NOTABLE,
                    message=f"'{layer.name}' is set at {float(size):g} pt, not {layer.size_pt:g} pt",
                )
            )
        leading = entry.get("leading_pt")
        wanted_leading = layer.leading_pt or round(layer.size_pt * 1.2, 2)
        if (
            leading is not None
            and wanted_leading
            and abs(float(leading) - wanted_leading) > SIZE_TOLERANCE_PT
        ):
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="leading_pt",
                    asked=wanted_leading,
                    produced=float(leading),
                    severity=Severity.NOTABLE,
                    message=f"'{layer.name}' leads at {float(leading):g} pt, not {wanted_leading:g} pt",
                )
            )
        tracking = entry.get("tracking")
        if tracking is not None and abs(float(tracking) - layer.tracking) > 1:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="tracking",
                    asked=layer.tracking,
                    produced=float(tracking),
                    severity=Severity.MINOR,
                    message=f"'{layer.name}' is tracked at {float(tracking):g}, not {layer.tracking:g}",
                )
            )
        alignment = entry.get("alignment")
        if alignment and str(alignment).lower() not in (layer.alignment, "fullyjustified"):
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="alignment",
                    asked=layer.alignment,
                    produced=str(alignment),
                    severity=Severity.NOTABLE,
                    message=f"'{layer.name}' is aligned {alignment}, not {layer.alignment}",
                )
            )
        font = entry.get("font")
        if font and not self._font_matches(layer, str(font)):
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="font",
                    asked=f"{layer.font} {layer.font_style}".strip(),
                    produced=str(font),
                    severity=Severity.BLOCKING,
                    message=f"'{layer.name}' is set in {font}, not {layer.font}",
                    suggestion=f"Install {layer.font}, or choose a face that is available.",
                )
            )

    def _check_appearance(self, layer: Layer, entry: dict[str, Any], out: FidelityReport) -> None:
        opacity = entry.get("opacity")
        if opacity is not None and abs(float(opacity) - layer.opacity) > 1:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="opacity",
                    asked=layer.opacity,
                    produced=float(opacity),
                    severity=Severity.NOTABLE,
                    message=f"'{layer.name}' is at {float(opacity):g}% opacity, not {layer.opacity:g}%",
                )
            )
        blend = entry.get("blend_mode")
        if blend and str(blend).replace("_", "") != layer.blend_mode.replace("_", ""):
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="blend_mode",
                    asked=layer.blend_mode,
                    produced=str(blend),
                    severity=Severity.NOTABLE,
                    message=f"'{layer.name}' blends as {blend}, not {layer.blend_mode}",
                )
            )
        distance = color_distance(layer.color, entry.get("color"))
        if distance is not None and distance > COLOR_TOLERANCE:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="color",
                    asked=layer.color,
                    produced=entry.get("color"),
                    severity=Severity.NOTABLE,
                    message=(
                        f"'{layer.name}' came out {entry.get('color')} rather than {layer.color} "
                        f"({distance} levels apart)"
                    ),
                    suggestion="A colour outside the document's gamut is clamped when it is applied.",
                )
            )
        if entry.get("visible") is False:
            out.differences.append(
                Difference(
                    layer=layer.name,
                    attribute="visible",
                    asked=True,
                    produced=False,
                    severity=Severity.BLOCKING,
                    message=f"'{layer.name}' is hidden in the finished design",
                )
            )

    @staticmethod
    def _font_matches(layer: Layer, produced: str) -> bool:
        """Whether the PostScript name the host reports is the face asked for."""
        if not layer.font:
            return True
        squeeze = produced.lower().replace(" ", "").replace("-", "")
        wanted = layer.font.lower().replace(" ", "").replace("-", "")
        if wanted in squeeze:
            return True
        return any(
            fallback.lower().replace(" ", "").replace("-", "") in squeeze for fallback in layer.fallback_fonts
        )

    def _looks_like_a_unit_mistake(self, layer: Layer, canvas: Box) -> bool:
        """Whether a box is the size millimetres would have given.

        A ratio threshold is the wrong test - a small badge on a poster is
        legitimately small. The tell is a contradiction that can be measured:
        a text frame that cannot hold one line of its own type, or a picture
        frame far below the resolution the format is produced at, which would
        make perfect sense if the numbers were millimetres.
        """
        dpi = self.plan.canvas.dpi
        as_px = Box.from_mm(layer.box.x, layer.box.y, layer.box.width, layer.box.height, dpi)
        if not canvas.contains(as_px, tolerance=canvas.width * UNIT_MISTAKE_HEADROOM):
            # Reading it as millimetres would run off the page, so whatever
            # else is wrong, that is not what happened.
            return False
        if layer.is_text and layer.size_pt:
            one_line_px = layer.size_pt / 72.0 * dpi
            return layer.box.height < one_line_px
        if layer.box.width <= 0 or canvas.width <= 0:
            return False
        # A frame under a hundredth of the sheet is not a design choice at
        # print resolution; a 250 px picture on a 3508 px poster is 21 mm.
        return layer.box.width / canvas.width < 0.01 or layer.box.height / canvas.height < 0.01
