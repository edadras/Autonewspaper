"""Persian and Arabic justification.

A Latin line is justified by stretching the spaces between words; an Arabic
one by elongating the joins inside them. Doing it the Latin way is the single
most recognisable sign that a page was set by something that did not know the
difference.
"""

from __future__ import annotations

import pytest

from app.utils import kashida as K

SENTENCE = "دولت برنامه اقتصادی تازه‌ای برای سال آینده اعلام کرد"


# ------------------------------------------------------------- the letters
@pytest.mark.parametrize("word", ["رود", "آزاد", "داد", "زر", "او"])
def test_a_word_of_non_connecting_letters_cannot_be_stretched(word):
    """Alef, dal, reh, zain and waw do not join to what follows them."""
    assert K.stretch(word, 3) == word


@pytest.mark.parametrize(
    ("word", "after"),
    [("دولت", "ل"), ("مجلس", "ج"), ("اقتصاد", "ق"), ("کشور", "ک")],
)
def test_a_tatweel_only_follows_a_letter_that_joins(word, after):
    stretched = K.stretch(word, 1)
    assert K.TATWEEL in stretched
    position = stretched.index(K.TATWEEL)
    assert stretched[position - 1] in K.CONNECTS_AFTER


def test_a_tatweel_never_lands_before_a_space_or_at_the_end():
    stretched = K.stretch(SENTENCE, 12)
    assert not stretched.endswith(K.TATWEEL)
    assert f"{K.TATWEEL} " not in stretched


def test_a_tatweel_never_lands_next_to_a_zero_width_non_joiner():
    """A ZWNJ is a deliberate break; elongating into it breaks the word."""
    stretched = K.stretch("تازه‌ای می‌رود خانه‌ها", 6)
    assert f"{K.TATWEEL}‌" not in stretched
    assert f"‌{K.TATWEEL}" not in stretched


def test_latin_and_numbers_are_left_alone():
    assert K.stretch("Government 2026", 4) == "Government 2026"
    assert K.stretch("۱۴۰۵ / ۰۵ / ۳۰", 4) == "۱۴۰۵ / ۰۵ / ۳۰"


def test_a_very_short_word_has_no_join_worth_stretching():
    assert K.stretch("به", 2) == "به"
    assert K.stretch("از", 2) == "از"


# ------------------------------------------------------------ the spread
def test_the_elongation_is_spread_across_words_before_doubling_up():
    """Otherwise one word becomes a rule with letters at each end."""
    stretched = K.stretch(SENTENCE, 4)
    stretched_words = [word for word in stretched.split() if K.TATWEEL in word]
    assert len(stretched_words) == 4, "four tatweels should reach four different words"


def test_one_word_never_takes_more_than_it_should():
    stretched = K.stretch("اقتصادی", 12, max_per_word=2)
    assert K.count(stretched) <= 2


def test_asking_for_more_than_there_is_room_for_stops_where_it_runs_out():
    stretched = K.stretch("دولت", 20)
    assert K.count(stretched) <= K.MAX_PER_WORD


def test_the_best_joins_are_used_first():
    """A kaf or a lam carries elongation; a beh looks stretched."""
    both = "کتاب"
    stretched = K.stretch(both, 1)
    position = stretched.index(K.TATWEEL)
    assert stretched[position - 1] == "ک", "the kaf is the graceful join here"


# ---------------------------------------------------------- what it means
def test_stretching_does_not_change_what_the_words_say():
    for count in range(1, 10):
        assert K.strip(K.stretch(SENTENCE, count)) == SENTENCE


def test_the_letters_themselves_are_untouched():
    stretched = K.stretch(SENTENCE, 8)
    assert K.strip(stretched) == SENTENCE
    assert len(stretched) == len(SENTENCE) + K.count(stretched)


# ------------------------------------------------------------ filling out
def test_a_line_is_filled_to_the_measure():
    """Each tatweel is one unit wide in this measure, so the sum is exact."""

    def measure(text: str) -> float:
        return float(len(text))

    line = "دولت برنامه اقتصادی اعلام کرد"
    filled = K.justify(line, measure=measure, target_width=len(line) + 4)

    assert K.count(filled) > 0
    assert len(filled) <= len(line) + 4, "it must not overrun the measure"
    assert len(filled) >= len(line) + 3, "and should get close to it"


def test_a_line_already_at_the_measure_is_left_alone():
    def measure(text: str) -> float:
        return float(len(text))

    line = "دولت برنامه اقتصادی"
    assert K.justify(line, measure=measure, target_width=len(line)) == line


def test_a_line_that_cannot_be_stretched_is_returned_as_it_is():
    def measure(text: str) -> float:
        return float(len(text))

    assert K.justify("رود او", measure=measure, target_width=40) == "رود او"
    assert K.justify("Latin text here", measure=measure, target_width=40) == "Latin text here"


def test_justification_never_overruns_even_by_one():
    def measure(text: str) -> float:
        return float(len(text))

    for target in range(20, 60):
        line = "کشور برنامه اقتصادی تازه اعلام کرد"
        filled = K.justify(line, measure=measure, target_width=target)
        assert len(filled) <= max(target, len(line))


# --------------------------------------------------------- on a real page
def test_a_justified_persian_column_is_actually_justified(template, article_blocks, tmp_path):
    """It was right-aligned and ragged, which is not justification."""
    from PIL import Image

    from app.layout.engine import LayoutEngine
    from app.models.schemas import ElementType
    from app.vision.renderer import PreviewRenderer

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {1: article_blocks}, page_count=1).pages[0]
    body = next(e for e in page.elements if e.type is ElementType.BODY)
    assert body.typography.alignment in ("justify", "justify_last_right")

    renderer = PreviewRenderer(template, dpi=110)
    rendered = renderer.render_page(page, tmp_path / "page.png")

    # Look at the ink in the body frame: a justified column reaches both
    # edges on most of its lines, a ragged one does not.
    # The body frame holds several columns; measure one of them, which is
    # what a line is actually justified to.
    count = max(1, body.typography.columns)
    gutter = body.typography.column_gutter_mm
    column_width = (body.rect.width - gutter * (count - 1)) / count
    # A right-to-left page fills from the right, so the first column is there.
    column_x = body.rect.right - column_width

    leading_px = body.typography.leading_pt / 72 * 110

    with Image.open(rendered.path) as image:
        grey = image.convert("L")
        scale = grey.width / page.width_mm
        left = int(column_x * scale) + 1
        right = int((column_x + column_width) * scale) - 1
        top = int(body.rect.y * scale)
        bottom = min(grey.height, int(body.rect.bottom * scale))
        strip = grey.crop((left, top, right, bottom))

        # Measure the ink of each line band, not each pixel row: a row
        # through the middle of the x-height says nothing on its own.
        filled: list[float] = []
        band = max(4, int(leading_px))
        for start_row in range(0, strip.height - band, band):
            xs = [
                x
                for x in range(strip.width)
                for y in range(start_row, start_row + band)
                if strip.getpixel((x, y)) < 160
            ]
            if len(xs) < 20:
                continue
            filled.append((max(xs) - min(xs)) / max(1, strip.width))

    assert filled, "the column drew no text at all"
    flush = sum(1 for share in filled if share > 0.9)
    assert flush / len(filled) > 0.55, (
        f"only {flush}/{len(filled)} lines fill the measure; a justified column fills most of them"
    )


def test_the_last_line_of_a_paragraph_is_not_filled_out(template):
    """Filling it is the giveaway that nothing understood the setting."""
    from app.vision.renderer import PreviewRenderer

    renderer = PreviewRenderer(template, dpi=110)
    from PIL import Image, ImageDraw

    from app.vision.fonts import load_font

    scratch = Image.new("L", (8, 8))
    draw = ImageDraw.Draw(scratch)
    font = load_font("IRANSans", 20, script="arabic")
    text = "دولت برنامه اقتصادی تازه‌ای اعلام کرد. " * 4
    marked = renderer._wrap_marked(text, font, 300, draw)
    scratch.close()

    assert marked, "nothing wrapped"
    assert marked[-1][1] is True, "the final line ends its paragraph"
    assert any(not ends for _line, ends in marked), "and the ones before it do not"


# ------------------------------------------------- and the same in InDesign
def test_indesign_is_told_to_use_kashida_for_justified_persian():
    """The preview inserting tatweels while InDesign had them off would mean
    the two engines produced visibly different columns."""
    from app.adobe.jsx import SCRIPTS_DIR

    source = (SCRIPTS_DIR / "indesign_lib.jsx").read_text(encoding="utf-8")
    block = source[source.index("api.ensureParagraphStyle") :][:3200]

    assert "DEFAULT_KASHIDAS" in block, "justified Arabic script needs kashida on"
    assert "KASHIDAS_OFF" in block, "and everything else needs it off"
    assert "justify" in block, "the choice is made by the alignment"


def test_persian_gets_persian_numerals_and_arabic_gets_arabic_ones():
    """۱۲۳ and ١٢٣ are different numerals; setting the wrong family is the
    kind of thing nobody notices until the paper is printed."""
    from app.adobe.jsx import SCRIPTS_DIR

    source = (SCRIPTS_DIR / "indesign_lib.jsx").read_text(encoding="utf-8")
    block = source[source.index("api.ensureParagraphStyle") :][:3200]

    assert "FARSI_DIGITS" in block
    assert 'spec.language === "ar"' in block


def test_the_paragraph_style_carries_the_language_not_only_the_direction():
    from pathlib import Path

    from app.adobe.jsx import typography_payload
    from app.layout.typography import TypographyEngine
    from app.models.schemas import ElementType
    from app.templates.schema import TemplateSpec

    template = TemplateSpec.load(
        Path(__file__).resolve().parents[2] / "templates" / "broadsheet_fa_standard.template.json"
    )
    for language, expected in (("fa", "fa"), ("ar", "ar")):
        typography = TypographyEngine(template, language).resolve(ElementType.BODY)
        assert typography_payload(typography, "body")["language"] == expected


def test_the_plan_text_is_never_written_with_tatweels_in_it(template, article_blocks):
    """The preview elongates as it draws; InDesign composes its own. Writing
    them into the plan would double them up."""
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    page = engine.plan_edition(1, {1: article_blocks}, page_count=1).pages[0]

    for element in page.elements:
        assert K.TATWEEL not in element.text, f"{element.id} carries a tatweel in the plan"
