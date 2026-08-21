"""Persian and Arabic justification.

A Latin line is justified by stretching the spaces between words. An Arabic
one is justified by elongating the joins *inside* words, with the tatweel
(U+0640) - which is why a justified Persian column has that even colour and
those long horizontal strokes, and why the same column justified the Latin
way looks gappy and wrong.

The rules are about which letters connect. A letter that does not join to the
following one - alef, dal, thal, reh, zain, jeh, waw and their relatives -
cannot be elongated after, because there is no join there to stretch. Where
there is a choice, some positions are better than others: the join before the
last letter of a word carries elongation best, and a kaf, lam, seen or sad
takes it more gracefully than a beh or a noon.

Nothing here changes what a word says. A tatweel is a rendering glyph: it
lengthens a join and leaves the letters, the spelling and the search text
alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

TATWEEL = "ـ"
ZWNJ = "‌"

#: Letters that never join to the letter that follows them, so a tatweel
#: cannot be placed after one.
NON_CONNECTING_AFTER = set("اأإآدذرزژوؤةى")

#: Letters that never join to the letter before them either. Nothing can be
#: elongated before one of these, because the join does not exist.
NON_CONNECTING_BEFORE: set[str] = set()

#: Letters after which a tatweel sits best, most graceful first. A kaf or a
#: lam has a long flat join; a beh or a noon has a shallow bowl and looks
#: stretched rather than elongated.
PREFERRED_AFTER = "كکگلمسشصضطظفقعغحخجچ"

#: Letters after which a tatweel is possible but plain.
ACCEPTABLE_AFTER = "بپتثنهيیئ"

#: Everything a tatweel may follow.
CONNECTS_AFTER = set(PREFERRED_AFTER) | set(ACCEPTABLE_AFTER)

#: Arabic-script letters, for deciding whether a word can be elongated at all.
_ARABIC_LETTER = re.compile(r"[ؠ-يٮ-ۓۺ-ۿ]")

#: How many tatweels one word may take. More than a few turns a word into a
#: rule with letters at each end.
MAX_PER_WORD = 3
#: And how many one line may take, as a share of its length.
MAX_PER_LINE_RATIO = 0.12
#: A word shorter than this has no join worth stretching.
MIN_WORD_LETTERS = 3


@dataclass(frozen=True)
class Opportunity:
    """One place in a line where a tatweel could go."""

    index: int
    """Where in the string the tatweel would be inserted."""
    word: int
    """Which word of the line it belongs to."""
    quality: float
    """0-1: how well this join carries elongation."""

    def __lt__(self, other: Opportunity) -> bool:
        return self.quality < other.quality


def is_arabic_word(word: str) -> bool:
    """Whether *word* is written in the Arabic script."""
    return bool(_ARABIC_LETTER.search(word))


def opportunities(text: str) -> list[Opportunity]:
    """Every place in *text* where a tatweel could legitimately go.

    Ordered by how well the join carries elongation, best first, so a caller
    that needs three can take the first three and get the three best.
    """
    found: list[Opportunity] = []
    word_index = 0
    word_start = 0
    letters_in_word = 0

    for index, character in enumerate(text):
        if character.isspace():
            word_index += 1
            word_start = index + 1
            letters_in_word = 0
            continue
        letters_in_word += 1
        # A join needs a letter on each side of it.
        following = text[index + 1] if index + 1 < len(text) else ""
        if not following or following.isspace() or following == ZWNJ:
            continue
        if character == ZWNJ or character not in CONNECTS_AFTER:
            continue
        if not _ARABIC_LETTER.match(following):
            continue
        word = _word_at(text, word_start)
        if len(word) < MIN_WORD_LETTERS:
            continue
        found.append(
            Opportunity(
                index=index + 1,
                word=word_index,
                quality=_quality(character, text, index, word, letters_in_word),
            )
        )
    return sorted(found, key=lambda item: -item.quality)


def _word_at(text: str, start: int) -> str:
    end = start
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end]


def _quality(character: str, text: str, index: int, word: str, position: int) -> float:
    """How well this join carries elongation, 0-1."""
    score = 0.75 if character in PREFERRED_AFTER else 0.4
    # The join before the last letter is where a scribe would stretch.
    remaining = len(word) - position
    if remaining == 1:
        score += 0.2
    elif remaining == 0:
        score -= 0.3
    # Not right at the start of a word: it looks like a mistake there.
    if position <= 1:
        score -= 0.25
    # Two elongations in a row read as one long rule.
    if index >= 1 and text[index - 1] == TATWEEL:
        score -= 0.5
    return max(0.0, min(1.0, score))


def stretch(text: str, count: int, *, max_per_word: int = MAX_PER_WORD) -> str:
    """Insert *count* tatweels into *text*, at its best joins.

    Spreads them across different words before doubling up in one, which is
    what keeps the line's colour even rather than putting a single very long
    word in the middle of it.
    """
    if count <= 0 or not is_arabic_word(text):
        return text
    candidates = opportunities(text)
    if not candidates:
        return text

    chosen: list[Opportunity] = []
    per_word: dict[int, int] = {}
    # Several passes, taking at most one per word each time, so the
    # elongation is spread before any word takes a second.
    for allowed in range(1, max_per_word + 1):
        for candidate in candidates:
            if len(chosen) >= count:
                break
            if per_word.get(candidate.word, 0) >= allowed:
                continue
            if candidate in chosen:
                continue
            chosen.append(candidate)
            per_word[candidate.word] = per_word.get(candidate.word, 0) + 1
        if len(chosen) >= count:
            break

    out = list(text)
    for candidate in sorted(chosen, key=lambda item: -item.index):
        out.insert(candidate.index, TATWEEL)
    return "".join(out)


def justify(
    text: str,
    *,
    measure: object,
    target_width: float,
    tolerance: float = 0.02,
    max_stretch: int | None = None,
) -> str:
    """Elongate *text* until it fills *target_width*.

    *measure* is any callable that returns the width of a string in the same
    units as *target_width*, so this works against a rendered font, a metric
    table or an estimate without knowing which.
    """
    if not callable(measure) or not is_arabic_word(text):
        return text
    current = float(measure(text))
    if current <= 0 or current >= target_width * (1 - tolerance):
        return text

    ceiling = max_stretch if max_stretch is not None else max(1, int(len(text) * MAX_PER_LINE_RATIO))
    best = text
    for count in range(1, ceiling + 1):
        candidate = stretch(text, count)
        if candidate == best:
            break
        width = float(measure(candidate))
        if width > target_width:
            # One too many: the previous one is as close as it gets without
            # overrunning the measure.
            return best
        best = candidate
        if width >= target_width * (1 - tolerance):
            return best
    return best


def strip(text: str) -> str:
    """Remove every tatweel, returning the text as it was written."""
    return (text or "").replace(TATWEEL, "")


def count(text: str) -> int:
    """How many tatweels are in *text*."""
    return (text or "").count(TATWEEL)
