"""Calendar handling for the edition date.

A Persian newspaper carries a Jalali (Solar Hijri) date on its masthead and
its folio; printing the Gregorian date there would be wrong however well the
digits are shaped. The conversion is arithmetic and exact, so it is done here
rather than pulled in as a dependency.
"""

from __future__ import annotations

from datetime import date

from app.utils.text import to_persian_digits

#: Solar Hijri month names, in Persian.
JALALI_MONTHS_FA = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

#: Persian weekday names, indexed by ``date.weekday()`` (Monday is 0).
WEEKDAYS_FA = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")

#: Days before the start of each Gregorian month in a non-leap year.
_GREGORIAN_MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_gregorian_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to the Solar Hijri (Jalali) calendar.

    The algorithm counts days from the Gregorian epoch and re-splits them on
    the Jalali year boundary, which is exact for the whole range the
    application can hold in a :class:`datetime.date`.
    """
    gy, gm, gd = year - 1600, month - 1, day - 1

    day_number = (
        365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400 + gd + sum(_GREGORIAN_MONTH_DAYS[:gm])
    )
    if gm > 1 and _is_gregorian_leap(year):
        day_number += 1

    # 79 is the offset between the two epochs; 12053 days is a 33-year cycle.
    day_number -= 79
    cycles, day_number = divmod(day_number, 12053)
    jy = 979 + 33 * cycles + 4 * (day_number // 1461)
    day_number %= 1461
    if day_number >= 366:
        jy += (day_number - 1) // 365
        day_number = (day_number - 1) % 365

    if day_number < 186:
        jm, jd = 1 + day_number // 31, 1 + day_number % 31
    else:
        rest = day_number - 186
        jm, jd = 7 + rest // 30, 1 + rest % 30
    return jy, jm, jd


def jalali_to_gregorian(year: int, month: int, day: int) -> date:
    """Convert a Solar Hijri date back to a Gregorian :class:`datetime.date`."""
    jy, jm, jd = year - 979, month - 1, day - 1
    day_number = 365 * jy + (jy // 33) * 8 + (jy % 33 + 3) // 4
    day_number += jm * 31 - (jm // 6) * (jm - 6) if jm < 7 else 186 + (jm - 6) * 30
    day_number += jd + 79

    gy = 1600 + 400 * (day_number // 146097)
    day_number %= 146097
    leap = True
    if day_number >= 36525:
        day_number -= 1
        gy += 100 * (day_number // 36524)
        day_number %= 36524
        if day_number >= 365:
            day_number += 1
        else:
            leap = False
    gy += 4 * (day_number // 1461)
    day_number %= 1461
    if day_number >= 366:
        leap = False
        day_number -= 1
        gy += day_number // 365
        day_number %= 365
    months = list(_GREGORIAN_MONTH_DAYS)
    if leap and _is_gregorian_leap(gy):
        months[1] = 29
    gm = 0
    while gm < 12 and day_number >= months[gm]:
        day_number -= months[gm]
        gm += 1
    return date(gy, gm + 1, day_number + 1)


def format_edition_date(value: date | str, language: str = "fa", *, long: bool = True) -> str:
    """Render *value* the way a masthead of that language would print it.

    Persian and Dari editions get the Jalali date in Persian digits; other
    languages keep the ISO date. A string that is not a plain ISO date is
    passed through untouched, so an operator can always type the exact wording
    they want.
    """
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip())
        except ValueError:
            return value
    if language not in ("fa", "prs"):
        return value.isoformat()
    jy, jm, jd = gregorian_to_jalali(value.year, value.month, value.day)
    if long:
        weekday = WEEKDAYS_FA[value.weekday()]
        text = f"{weekday} {jd} {JALALI_MONTHS_FA[jm - 1]} {jy}"
    else:
        text = f"{jy}/{jm:02d}/{jd:02d}"
    return to_persian_digits(text)
