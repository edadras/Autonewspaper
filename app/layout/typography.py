"""Typography engine.

Resolves a template's paragraph styles into concrete
:class:`~app.models.schemas.TypographySpec` values for each frame, fits
display type to its frame, decides when body text must be trimmed or its size
reduced, and applies the RTL/Persian rules (direction, alignment, digits,
font fallback) required by specification §37 and §38.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.schemas import ElementType, Rect, TypographySpec
from app.templates.schema import ParagraphStyleSpec, TemplateSpec
from app.utils import text as T
from app.utils.units import mm_to_pt, pt_to_mm

log = logging.getLogger(__name__)

RTL_LANGUAGES = {"fa", "ar", "ur", "he"}

RUNNING_TEXT = {
    ElementType.BODY,
    ElementType.LEAD,
    ElementType.SIDEBAR,
    ElementType.CAPTION,
    ElementType.BYLINE,
}

DEFAULT_FALLBACKS = {
    "fa": ["Vazirmatn", "IRANSans", "Sahel", "Tahoma", "Arial"],
    "ar": ["Noto Naskh Arabic", "Tahoma", "Arial"],
    "tr": ["Inter", "Segoe UI", "Arial"],
    "en": ["Source Serif Pro", "Georgia", "Times New Roman"],
}


@dataclass
class FitResult:
    """Outcome of fitting text into a frame."""

    typography: TypographySpec
    text: str
    overflow: float
    """``0`` when the text fits; ``0.25`` means a quarter too much."""
    lines: int
    truncated: bool = False
    used_min_size: bool = False

    @property
    def fits(self) -> bool:
        """Whether the text fits at the resolved size."""
        return self.overflow <= 0.001


class TypographyEngine:
    """Resolves and fits type for a given template."""

    def __init__(self, template: TemplateSpec, language: str | None = None) -> None:
        self.template = template
        self.language = language or template.language
        self.direction = "rtl" if self.language in RTL_LANGUAGES else "ltr"

    # ------------------------------------------------------------- styles
    def style_spec(self, element_type: ElementType) -> ParagraphStyleSpec:
        """Template paragraph style backing *element_type*.

        Running-text styles are floored at the template's own
        ``layout_rules.min_body_size_pt`` so auto-fitting can never shrink body
        copy below the size the design system declares readable.
        """
        spec = self.template.style_for(element_type)
        floor = self.template.layout_rules.min_body_size_pt
        if element_type in RUNNING_TEXT and spec.min_size_pt < floor:
            spec = spec.model_copy(update={"min_size_pt": floor})
        return spec

    def resolve(
        self,
        element_type: ElementType,
        *,
        columns: int = 1,
        scale: float = 1.0,
        color: str | None = None,
    ) -> TypographySpec:
        """Concrete typography for a frame of *element_type*."""
        spec = self.style_spec(element_type)
        typography = TypographySpec(
            style_name=spec.style_name,
            font_family=spec.font_family or self.template.fonts.get("body", "IRANSans"),
            font_style=spec.font_style,
            size_pt=spec.clamp(spec.size_pt * scale),
            leading_pt=max(spec.size_pt * scale * 1.05, spec.leading_pt * scale),
            tracking=spec.tracking,
            alignment=spec.alignment,
            direction=self.direction,
            color=color or spec.color,
            space_before_pt=spec.space_before_pt,
            space_after_pt=spec.space_after_pt,
            hyphenation=spec.hyphenation and self.direction == "ltr",
            columns=max(1, columns),
            column_gutter_mm=self.template.grid.gutter_mm,
            all_caps=spec.all_caps and self.direction == "ltr",
            fallback_fonts=self.fallbacks(spec.font_family),
        )
        if self.direction == "rtl" and typography.alignment == "left":
            typography.alignment = "right"
        elif self.direction == "ltr" and typography.alignment == "right":
            typography.alignment = "left"
        return typography

    def fallbacks(self, font_family: str) -> list[str]:
        """Fallback chain for a font, template first then language defaults."""
        chain = list(self.template.font_fallbacks.get(font_family, []))
        for candidate in DEFAULT_FALLBACKS.get(self.language, []):
            if candidate != font_family and candidate not in chain:
                chain.append(candidate)
        return chain

    def prepare(self, raw: str, element_type: ElementType) -> str:
        """Normalise text for typesetting (Persian letters, spacing, digits)."""
        if not raw:
            return ""
        normalized = T.normalize(raw, self.language)
        if element_type in (ElementType.HEADLINE, ElementType.SUBHEADLINE, ElementType.KICKER):
            normalized = normalized.replace("\n", " ").strip(" .،,;:")
        if self.language == "fa" and element_type in (ElementType.FOLIO, ElementType.CAPTION):
            normalized = T.to_persian_digits(normalized)
        return normalized

    # ---------------------------------------------------------------- fit
    def fit(
        self,
        raw_text: str,
        rect: Rect,
        element_type: ElementType,
        *,
        columns: int = 1,
        allow_truncate: bool = False,
        min_scale: float = 0.72,
    ) -> FitResult:
        """Fit *raw_text* into *rect*, shrinking type before trimming words.

        The engine first tries the template size, then steps the size down to
        the style's own minimum, and only then - if *allow_truncate* - drops
        trailing words. The returned :attr:`FitResult.overflow` is what the
        constraint checker and the QA scorer use before InDesign reports the
        real overflow.
        """
        spec = self.style_spec(element_type)
        text = self.prepare(raw_text, element_type)
        typography = self.resolve(element_type, columns=columns)
        if not text or rect.width <= 0 or rect.height <= 0:
            return FitResult(typography=typography, text=text, overflow=0.0, lines=0)

        usable_height = rect.height - pt_to_mm(spec.space_before_pt + spec.space_after_pt)
        best: FitResult | None = None
        scale = 1.0
        while scale >= min_scale - 1e-6:
            candidate = self.resolve(element_type, columns=columns, scale=scale)
            if candidate.size_pt < spec.min_size_pt - 1e-6:
                break
            lines = T.estimate_lines(
                text,
                rect.width,
                candidate.size_pt,
                columns=columns,
                gutter_mm=typography.column_gutter_mm,
                language=self.language,
            )
            needed_mm = lines * pt_to_mm(candidate.leading_pt)
            overflow = max(0.0, (needed_mm - usable_height) / max(1e-6, usable_height))
            result = FitResult(
                typography=candidate,
                text=text,
                overflow=round(overflow, 4),
                lines=lines,
                used_min_size=abs(candidate.size_pt - spec.min_size_pt) < 0.01,
            )
            if best is None or result.overflow < best.overflow:
                best = result
            if result.fits:
                return result
            if not spec.auto_fit:
                break
            scale -= 0.04

        assert best is not None
        if allow_truncate and best.overflow > 0:
            words = T.fit_words(
                text,
                rect.width,
                usable_height,
                best.typography.size_pt,
                best.typography.leading_pt,
                columns=columns,
                gutter_mm=typography.column_gutter_mm,
                language=self.language,
            )
            if words > 0:
                trimmed = " ".join(text.split()[:words])
                return FitResult(
                    typography=best.typography,
                    text=trimmed,
                    overflow=0.0,
                    lines=T.estimate_lines(
                        trimmed, rect.width, best.typography.size_pt,
                        columns=columns, gutter_mm=typography.column_gutter_mm,
                        language=self.language,
                    ),
                    truncated=True,
                    used_min_size=best.used_min_size,
                )
        return best

    def fit_display(
        self,
        raw_text: str,
        rect: Rect,
        element_type: ElementType = ElementType.HEADLINE,
        *,
        max_lines: int = 3,
        size_cap_pt: float | None = None,
    ) -> FitResult:
        """Size display type so it fills its frame in at most *max_lines* lines.

        *size_cap_pt* lets the caller hold a secondary story's headline below
        the lead's, which is what produces a readable hierarchy on the page.
        """
        spec = self.style_spec(element_type)
        text = self.prepare(raw_text, element_type)
        if not text:
            return FitResult(self.resolve(element_type), "", 0.0, 0)

        best = FitResult(self.resolve(element_type), text, 1.0, 0)
        size = min(spec.max_size_pt, size_cap_pt) if size_cap_pt else spec.max_size_pt
        size = max(size, spec.min_size_pt)
        while size >= spec.min_size_pt:
            typography = self.resolve(element_type)
            typography.size_pt = round(size, 2)
            typography.leading_pt = round(size * (spec.leading_pt / max(1e-6, spec.size_pt)), 2)
            lines = T.estimate_lines(text, rect.width, size, language=self.language)
            needed = lines * pt_to_mm(typography.leading_pt)
            if lines <= max_lines and needed <= rect.height:
                return FitResult(typography=typography, text=text, overflow=0.0, lines=lines)
            overflow = max(0.0, (needed - rect.height) / max(1e-6, rect.height))
            if overflow < best.overflow:
                best = FitResult(typography, text, round(overflow, 4), lines)
            size -= max(0.5, size * 0.06)

        # Nothing fits: keep the minimum size and shorten the headline instead.
        typography = self.resolve(element_type)
        typography.size_pt = spec.min_size_pt
        typography.leading_pt = round(spec.min_size_pt * 1.1, 2)
        chars = self._chars_that_fit(rect, typography, max_lines)
        shortened = T.truncate_chars(text, chars) if chars < len(text) else text
        return FitResult(
            typography=typography,
            text=shortened,
            overflow=0.0 if chars < len(text) else best.overflow,
            lines=T.estimate_lines(shortened, rect.width, typography.size_pt, language=self.language),
            truncated=chars < len(text),
            used_min_size=True,
        )

    def _chars_that_fit(self, rect: Rect, typography: TypographySpec, max_lines: int) -> int:
        em_mm = typography.size_pt * 25.4 / 72.0
        advance = em_mm * (0.46 if self.direction == "rtl" else 0.50)
        per_line = max(1, int(rect.width / max(1e-6, advance)))
        possible_lines = min(max_lines, max(1, int(rect.height / pt_to_mm(typography.leading_pt))))
        return per_line * possible_lines

    # ---------------------------------------------------------- estimates
    def height_for(
        self, text: str, width_mm: float, element_type: ElementType, columns: int = 1
    ) -> float:
        """Height in millimetres needed to typeset *text* at the default size."""
        typography = self.resolve(element_type, columns=columns)
        return T.estimate_text_height_mm(
            self.prepare(text, element_type),
            width_mm,
            typography.size_pt,
            typography.leading_pt,
            columns=columns,
            gutter_mm=typography.column_gutter_mm,
            language=self.language,
        ) + pt_to_mm(typography.space_before_pt + typography.space_after_pt)

    def words_per_mm2(self, element_type: ElementType = ElementType.BODY) -> float:
        """Rough text density, used to size body frames from a word count."""
        typography = self.resolve(element_type)
        em_mm = typography.size_pt * 25.4 / 72.0
        advance = em_mm * (0.46 if self.direction == "rtl" else 0.50)
        line_mm = pt_to_mm(typography.leading_pt)
        average_word_chars = 5.5 if self.direction == "rtl" else 5.8
        return 1.0 / max(1e-6, advance * average_word_chars * line_mm)

    def area_for_words(self, words: int, element_type: ElementType = ElementType.BODY) -> float:
        """Area in mm² needed for *words* of body text."""
        return words / max(1e-6, self.words_per_mm2(element_type))

    def minimum_readable_size(self) -> float:
        """Smallest body size the template permits."""
        return max(self.template.layout_rules.min_body_size_pt, self.style_spec(ElementType.BODY).min_size_pt)

    def describe(self) -> dict[str, object]:
        """Summary for the layout inspector."""
        body = self.resolve(ElementType.BODY)
        headline = self.resolve(ElementType.HEADLINE)
        return {
            "language": self.language,
            "direction": self.direction,
            "body": f"{body.font_family} {body.size_pt}/{body.leading_pt}pt",
            "headline": f"{headline.font_family} {headline.size_pt}/{headline.leading_pt}pt",
            "fallbacks": body.fallback_fonts,
            "min_body_pt": self.minimum_readable_size(),
            "words_per_cm2": round(self.words_per_mm2() * 100, 2),
        }
