"""Unicode, RTL and Persian text utilities.

The application is Unicode-first. Persian, Arabic, Turkish and English are
first-class: the same helpers normalise digits, detect direction, estimate
typeset length and shape text for frames that mix RTL and LTR runs.
"""

from __future__ import annotations

import re
import unicodedata

ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
LATIN_DIGITS = "0123456789"

RTL_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB1D, 0xFDFF),  # Hebrew/Arabic presentation forms
    (0xFE70, 0xFEFF),  # Arabic presentation forms-B
)

PERSIAN_ONLY_LETTERS = "پچژگک"
ARABIC_ONLY_LETTERS = "ةيكإأؤئ"
PERSIAN_MARKER_WORDS = {
    "است",
    "این",
    "که",
    "را",
    "می",
    "های",
    "شد",
    "بود",
    "برای",
    "با",
    "از",
    "در",
    "کرد",
    "خود",
    "آن",
    "هم",
    "یک",
    "تا",
    "رخ",
    "داد",
}
ARABIC_MARKER_WORDS = {
    "في",
    "من",
    "على",
    "الذي",
    "هذا",
    "هذه",
    "التي",
    "كان",
    "إلى",
    "عن",
    "قال",
    "مع",
    "بعد",
    "ذلك",
    "بالعالم",
    "مرحبا",
}

RLM = "‏"
LRM = "‎"
ZWNJ = "‌"

_PERSIAN_FIXES = {
    "ي": "ی",  # Arabic yeh -> Farsi yeh
    "ك": "ک",  # Arabic kaf -> Keheh
    "ة": "ه",  # teh marbuta -> heh
    "ۀ": "ه‌",  # heh with yeh above
}

_PUNCT_FA = {",": "،", ";": "؛", "?": "؟"}

# ZWNJ (U+200C) joins the parts of one Persian word, so it must not split
# tokens: "خیابان\u200cها" is a single word, not two.
_WORD_RE = re.compile(r"\S+", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?؟…\n]+[.!?؟…]?", re.UNICODE)
_WS_RE = re.compile(r"[ \t ]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def is_rtl_char(char: str) -> bool:
    """Whether *char* belongs to a right-to-left script."""
    code = ord(char)
    return any(low <= code <= high for low, high in RTL_RANGES)


def rtl_ratio(text: str) -> float:
    """Fraction of letters in *text* that are right-to-left."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if is_rtl_char(c)) / len(letters)


def detect_direction(text: str) -> str:
    """Return ``"rtl"`` or ``"ltr"`` for *text*."""
    return "rtl" if rtl_ratio(text) >= 0.3 else "ltr"


def detect_language(text: str) -> str:
    """Script-based language guess (``fa``/``ar``/``tr``/``en``).

    Persian and Arabic share an alphabet, so the two are separated by counting
    letters and function words that only one of them uses, rather than by
    looking for a single marker letter that many Persian sentences lack.
    """
    if not text.strip():
        return "en"
    if rtl_ratio(text) >= 0.3:
        persian = sum(text.count(ch) for ch in PERSIAN_ONLY_LETTERS)
        arabic = sum(text.count(ch) for ch in ARABIC_ONLY_LETTERS)
        tokens = set(_WORD_RE.findall(text))
        persian += 2 * len(tokens & PERSIAN_MARKER_WORDS)
        arabic += 2 * len(tokens & ARABIC_MARKER_WORDS)
        return "ar" if arabic > persian else "fa"
    turkish_only = set("ğışİĞİŞÇÖÜçöü")
    if any(c in turkish_only for c in text):
        return "tr"
    return "en"


def to_latin_digits(text: str) -> str:
    """Convert Persian/Arabic-Indic digits to ASCII digits."""
    table = str.maketrans(PERSIAN_DIGITS + ARABIC_INDIC_DIGITS, LATIN_DIGITS * 2)
    return text.translate(table)


def to_persian_digits(text: str) -> str:
    """Convert ASCII digits to Persian digits."""
    return text.translate(str.maketrans(LATIN_DIGITS, PERSIAN_DIGITS))


def normalize_persian(text: str, *, digits: bool = False, punctuation: bool = True) -> str:
    """Normalise Persian text (letters, spacing, optional digits/punctuation)."""
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text)
    for src, dst in _PERSIAN_FIXES.items():
        out = out.replace(src, dst)
    out = out.replace("​", "").replace("﻿", "")
    if punctuation:
        for src, dst in _PUNCT_FA.items():
            out = out.replace(src, dst)
    if digits:
        out = to_persian_digits(to_latin_digits(out))
    out = _WS_RE.sub(" ", out)
    out = _MULTI_NL_RE.sub("\n\n", out)
    return out.strip()


def normalize_arabic(text: str) -> str:
    """Normalise Arabic text without Persian-ising its letters.

    The Persian rules rewrite Arabic yeh, kaf and teh marbuta into their
    Persian counterparts, which changes how Arabic words are spelt - running
    them over an Arabic edition turns "الحكومة" into "الحكومه". Arabic gets
    the shared cleanup only: composition, invisible marks and whitespace.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text)
    out = out.replace("\u200b", "").replace("\ufeff", "")
    out = _WS_RE.sub(" ", out)
    return _MULTI_NL_RE.sub("\n\n", out).strip()


def normalize(text: str, language: str = "fa") -> str:
    """Language-aware normalisation entry point."""
    if language == "fa":
        return normalize_persian(text)
    if language in ("ar", "ur", "he"):
        return normalize_arabic(text)
    return _MULTI_NL_RE.sub("\n\n", _WS_RE.sub(" ", unicodedata.normalize("NFC", text))).strip()


def word_count(text: str) -> int:
    """Number of words, ZWNJ-aware."""
    return len(_WORD_RE.findall(text or ""))


def char_count(text: str, *, spaces: bool = True) -> int:
    """Number of characters, optionally excluding whitespace."""
    if spaces:
        return len(text or "")
    return len(re.sub(r"\s", "", text or ""))


def sentences(text: str) -> list[str]:
    """Split *text* into sentences (handles Persian punctuation)."""
    return [s.strip() for s in _SENTENCE_RE.findall(text or "") if s.strip()]


def truncate_words(text: str, limit: int, ellipsis: str = "…") -> str:
    """Truncate *text* to *limit* words."""
    words = _WORD_RE.findall(text or "")
    if len(words) <= limit:
        return text.strip()
    return " ".join(words[:limit]).rstrip("،,;:") + ellipsis


def truncate_chars(text: str, limit: int, ellipsis: str = "…") -> str:
    """Truncate *text* to *limit* characters on a word boundary."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip("،,;: ") + ellipsis


def summarize(text: str, max_words: int = 45) -> str:
    """Extractive summary: leading sentences up to *max_words*."""
    out: list[str] = []
    used = 0
    for sentence in sentences(text):
        count = word_count(sentence)
        if used + count > max_words and out:
            break
        out.append(sentence)
        used += count
        if used >= max_words:
            break
    return " ".join(out) if out else truncate_words(text, max_words)


def lead_paragraph(text: str, max_words: int = 35) -> str:
    """The first paragraph, trimmed to *max_words*."""
    for paragraph in (text or "").split("\n"):
        if paragraph.strip():
            return truncate_words(paragraph.strip(), max_words)
    return ""


def keywords(text: str, limit: int = 8, min_length: int = 3) -> list[str]:
    """Frequency-based keyword extraction with Persian/English stop-words."""
    stop = STOPWORDS_FA | STOPWORDS_EN
    counts: dict[str, int] = {}
    for raw in _WORD_RE.findall(normalize_persian(text or "", punctuation=False).lower()):
        token = raw.strip("«»\"'()[]{}،,.:;!?؟-–—")
        if len(token) < min_length or token in stop or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def bidi_wrap(text: str, direction: str = "rtl") -> str:
    """Wrap *text* in the marks that keep mixed RTL/LTR runs correct."""
    mark = RLM if direction == "rtl" else LRM
    return f"{mark}{text}{mark}"


def clean_headline(text: str, language: str = "fa", max_chars: int = 70) -> str:
    """Normalise, strip trailing punctuation and cap the length of a headline."""
    out = normalize(text, language).replace("\n", " ").strip(" .،,;:!")
    return truncate_chars(out, max_chars, ellipsis="")


def estimate_lines(
    text: str,
    frame_width_mm: float,
    font_size_pt: float,
    *,
    columns: int = 1,
    gutter_mm: float = 4.0,
    language: str = "fa",
) -> int:
    """Estimate how many typeset lines *text* needs in a frame.

    The estimate uses an average glyph advance derived from the point size and
    the script: Arabic-script faces average ~0.46 em per glyph, Latin ~0.50 em.
    It is intentionally conservative - InDesign remains the source of truth and
    reports real overflow, but the planner needs a number before the document
    exists.
    """
    if not text or frame_width_mm <= 0 or font_size_pt <= 0:
        return 0
    column_width_mm = (frame_width_mm - gutter_mm * (columns - 1)) / max(1, columns)
    if column_width_mm <= 0:
        return 0
    em_mm = font_size_pt * 25.4 / 72.0
    advance = em_mm * (0.46 if language in ("fa", "ar") else 0.50)
    chars_per_line = max(1.0, column_width_mm / advance)
    total = 0
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            total += 1
            continue
        length = len(paragraph)
        total += max(1, int(-(-length // chars_per_line)))
    return max(1, -(-total // max(1, columns)))


def estimate_text_height_mm(
    text: str,
    frame_width_mm: float,
    font_size_pt: float,
    leading_pt: float,
    *,
    columns: int = 1,
    gutter_mm: float = 4.0,
    language: str = "fa",
) -> float:
    """Estimated height in millimetres needed to typeset *text*."""
    lines = estimate_lines(
        text,
        frame_width_mm,
        font_size_pt,
        columns=columns,
        gutter_mm=gutter_mm,
        language=language,
    )
    return lines * leading_pt * 25.4 / 72.0


def fit_words(
    text: str,
    frame_width_mm: float,
    frame_height_mm: float,
    font_size_pt: float,
    leading_pt: float,
    *,
    columns: int = 1,
    gutter_mm: float = 4.0,
    language: str = "fa",
) -> int:
    """How many words of *text* fit in the given frame (estimate)."""
    if not text:
        return 0
    lines_available = int(frame_height_mm / (leading_pt * 25.4 / 72.0))
    if lines_available <= 0:
        return 0
    words = _WORD_RE.findall(text)
    low, high = 0, len(words)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = " ".join(words[:mid])
        needed = estimate_lines(
            candidate,
            frame_width_mm,
            font_size_pt,
            columns=columns,
            gutter_mm=gutter_mm,
            language=language,
        )
        if needed <= lines_available:
            low = mid
        else:
            high = mid - 1
    return low


def escape_jsx(text: str) -> str:
    """Escape *text* for embedding in an ExtendScript string literal."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\r")
        .replace("\n", "\\r")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


STOPWORDS_FA = {
    "از",
    "به",
    "با",
    "برای",
    "که",
    "این",
    "آن",
    "را",
    "در",
    "و",
    "یا",
    "تا",
    "بر",
    "هم",
    "است",
    "بود",
    "شد",
    "شده",
    "می",
    "های",
    "ها",
    "یک",
    "خود",
    "هر",
    "نیز",
    "اما",
    "اگر",
    "دیگر",
    "کرد",
    "کند",
    "کرده",
    "باید",
    "چون",
    "وی",
    "او",
    "ما",
    "شما",
    "آنها",
    "روی",
    "بین",
    "طور",
    "همه",
    "بیش",
    "کمتر",
    "بیشتر",
    "درباره",
    "توسط",
    "پس",
    "قبل",
    "بعد",
}

STOPWORDS_EN = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "this",
    "from",
    "have",
    "has",
    "was",
    "were",
    "are",
    "but",
    "not",
    "you",
    "all",
    "his",
    "her",
    "its",
    "they",
    "them",
    "will",
    "would",
    "could",
    "should",
    "been",
    "into",
    "than",
    "then",
    "there",
    "their",
    "about",
    "after",
    "before",
    "over",
    "under",
    "more",
    "most",
    "some",
    "such",
}
