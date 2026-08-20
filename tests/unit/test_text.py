"""Unicode, Persian and RTL text handling."""

from __future__ import annotations

import pytest

from app.utils import text as T


def test_direction_and_language_detection():
    assert T.detect_direction("سلام دنیا") == "rtl"
    assert T.detect_direction("Hello world") == "ltr"
    assert T.detect_language("زلزله در تهران رخ داد") == "fa"
    assert T.detect_language("Hello there") == "en"
    assert T.detect_language("مرحبا بالعالم") == "ar"


def test_persian_normalisation_fixes_arabic_letters():
    normalised = T.normalize_persian("كتاب يك")
    assert "ك" not in normalised
    assert "ي" not in normalised
    assert normalised == "کتاب یک"


def test_digit_conversion_round_trips():
    assert T.to_latin_digits("۱۴۰۵/۰۵/۲۹") == "1405/05/29"
    assert T.to_persian_digits("2026") == "۲۰۲۶"


def test_word_count_is_zwnj_aware():
    assert T.word_count("خیابان‌ها") == 1
    assert T.word_count("یک دو سه") == 3


def test_clean_headline_strips_punctuation_and_caps_length():
    headline = T.clean_headline("  یک تیتر آزمایشی، برای تست.  ", "fa", 20)
    assert not headline.endswith(".")
    assert len(headline) <= 20


@pytest.mark.parametrize("language", ["fa", "en"])
def test_line_estimation_grows_with_text(language):
    short = T.estimate_lines("word " * 20, 80, 9.5, language=language)
    long = T.estimate_lines("word " * 200, 80, 9.5, language=language)
    assert 0 < short < long


def test_fit_words_never_exceeds_the_frame():
    body = "کلمه " * 500
    words = T.fit_words(body, 80.0, 60.0, 9.5, 13.0, columns=2, language="fa")
    assert 0 < words < 500
    fitted = " ".join(body.split()[:words])
    height = T.estimate_text_height_mm(fitted, 80.0, 9.5, 13.0, columns=2, language="fa")
    assert height <= 60.0 + 1e-6


def test_escape_jsx_quotes_and_newlines():
    escaped = T.escape_jsx('a "quote"\nsecond')
    assert '\\"' in escaped
    assert "\\r" in escaped
    assert "\n" not in escaped


def test_keywords_exclude_stopwords():
    keywords = T.keywords("این یک متن است درباره زلزله و امدادرسانی در منطقه", 5)
    assert "این" not in keywords
    assert any(word in keywords for word in ("زلزله", "امدادرسانی", "منطقه"))


# ------------------------------------------------------------- the calendar
class TestEditionDate:
    """A Persian masthead carries a Jalali date, not a Gregorian one."""

    @pytest.mark.parametrize(
        ("gregorian", "jalali"),
        [
            ((2024, 8, 20), (1403, 5, 30)),
            ((2021, 3, 21), (1400, 1, 1)),  # Nowruz
            ((2022, 3, 20), (1400, 12, 29)),  # the day before
            ((2025, 3, 21), (1404, 1, 1)),
            ((1979, 2, 11), (1357, 11, 22)),
            ((2000, 1, 1), (1378, 10, 11)),
        ],
    )
    def test_known_dates_convert(self, gregorian, jalali):
        from app.utils.dates import gregorian_to_jalali

        assert gregorian_to_jalali(*gregorian) == jalali

    def test_the_conversion_round_trips_over_a_long_span(self):
        """Every day for ninety years, not a handful of samples."""
        from datetime import date, timedelta

        from app.utils.dates import gregorian_to_jalali, jalali_to_gregorian

        day = date(1970, 1, 1)
        end = date(2060, 1, 1)
        while day < end:
            assert jalali_to_gregorian(*gregorian_to_jalali(day.year, day.month, day.day)) == day
            day += timedelta(days=365 if day.month != 3 else 1)

    def test_a_persian_masthead_gets_the_jalali_date_in_persian_digits(self):
        from app.utils.dates import format_edition_date

        assert format_edition_date("2024-08-20", "fa") == "سه‌شنبه ۳۰ مرداد ۱۴۰۳"
        assert format_edition_date("2024-08-20", "fa", long=False) == "۱۴۰۳/۰۵/۳۰"

    def test_other_languages_keep_the_gregorian_date(self):
        from app.utils.dates import format_edition_date

        assert format_edition_date("2024-08-20", "en") == "2024-08-20"
        assert format_edition_date("2024-08-20", "tr") == "2024-08-20"

    def test_wording_the_operator_typed_is_left_alone(self):
        from app.utils.dates import format_edition_date

        assert format_edition_date("ویژه‌نامه نوروز", "fa") == "ویژه‌نامه نوروز"


def test_the_folio_of_a_persian_page_carries_the_jalali_date(template, article_blocks):
    """The whole point of the conversion is what reaches the page."""
    from app.layout.engine import LayoutEngine

    engine = LayoutEngine(template, language="fa")
    plan = engine.plan_edition(
        1,
        {1: article_blocks},
        page_count=1,
        publication_name="صبح ایران",
        edition_date="2024-08-20",
    )
    folios = [e.text for e in plan.pages[0].elements if e.type.value == "folio"]

    assert folios
    assert "۱۴۰۳" in folios[0]
    assert "مرداد" in folios[0]
    assert "2024" not in folios[0]
