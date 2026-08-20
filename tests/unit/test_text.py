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
