"""Measuring a page.

Everything here works on the pixels of one page and reports in millimetres,
because that is what a template is written in. There is no model involved:
a column gutter is a run of white the width of the live area, and a keyline
box is a rectangle whose edge is inked and whose middle is not. Saying so in
code makes the result reproducible and, when it is wrong, debuggable.

The profiles are taken with PIL's own box filter rather than a pixel loop -
resizing an ink mask to one pixel wide gives the mean ink per row in a single
call, which is the difference between a page taking a second and a minute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageFilter

from app.harvest.reader import PageRaster

log = logging.getLogger(__name__)

#: Below this, a pixel counts as ink rather than paper. Newsprint is not
#: white and a tint panel is not ink, so the threshold is measured against
#: the page's own paper colour rather than fixed.
INK_MARGIN = 26

#: A row or column is "empty" when this little of it is inked. Not zero: a
#: gutter can carry a stray descender or a scanning artefact.
EMPTY_SHARE = 0.012

#: Under this, a mark is a word rather than a rule. Persian is a connected
#: script, so a single word can be a centimetre of unbroken ink; a rule runs
#: the width of a column.
MIN_RULE_MM = 26.0
MAX_RULE_MM = 2.2

#: A rule is *thin for its length*: a hairline sixty millimetres long is two
#: hundred times its own thickness, where a line of type is twenty. This is
#: what tells the two apart, and it only works if the thickness is measured
#: over the run's own span - which is why :func:`_thickness` exists.
MIN_RULE_ASPECT = 40.0

#: A panel worth reporting. Smaller than this is a bullet, not a box.
MIN_PANEL_MM = 16.0

#: Narrower than this is the space between two words, not a gutter.
MIN_GUTTER_MM = 1.6

#: How square-cornered a region has to be before it is called a box.
PANEL_FILL = 0.80


@dataclass
class Rule:
    """A printed line: the rules that separate one story from the next."""

    x_mm: float
    y_mm: float
    length_mm: float
    weight_pt: float
    orientation: str
    color: str = "#111111"

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "x_mm": round(self.x_mm, 1),
            "y_mm": round(self.y_mm, 1),
            "length_mm": round(self.length_mm, 1),
            "weight_pt": round(self.weight_pt, 2),
            "orientation": self.orientation,
            "color": self.color,
        }


@dataclass
class Panel:
    """A box on the page - the کادر a sidebar or a fact list sits in."""

    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    color: str
    keyline: bool = False
    keyline_pt: float = 0.0
    keyline_color: str = "#111111"
    corner_mm: float = 0.0
    coverage: float = 1.0
    """How much of its bounding box the region actually fills."""
    holds_a_picture: bool = False
    """Whether what is inside it varies - a photograph rather than a tint."""

    @property
    def area_mm2(self) -> float:
        """Area, for ranking one panel against another."""
        return self.width_mm * self.height_mm

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "x_mm": round(self.x_mm, 1),
            "y_mm": round(self.y_mm, 1),
            "width_mm": round(self.width_mm, 1),
            "height_mm": round(self.height_mm, 1),
            "color": self.color,
            "keyline": self.keyline,
            "keyline_pt": round(self.keyline_pt, 2),
            "keyline_color": self.keyline_color,
            "corner_mm": round(self.corner_mm, 1),
            "coverage": round(self.coverage, 2),
            "holds_a_picture": self.holds_a_picture,
        }


@dataclass
class TypeBand:
    """One line of type, measured off the page."""

    y_mm: float
    height_mm: float
    size_pt: float
    """The band's own depth in points, which is close to the type size."""
    leading_pt: float = 0.0
    ink: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "y_mm": round(self.y_mm, 1),
            "height_mm": round(self.height_mm, 2),
            "size_pt": round(self.size_pt, 1),
            "leading_pt": round(self.leading_pt, 1),
        }


@dataclass
class PageMeasurements:
    """Everything one page turned out to say."""

    index: int
    width_mm: float
    height_mm: float
    margin_top_mm: float = 0.0
    margin_bottom_mm: float = 0.0
    margin_left_mm: float = 0.0
    margin_right_mm: float = 0.0
    columns: int = 0
    gutter_mm: float = 0.0
    column_width_mm: float = 0.0
    paper: str = "#ffffff"
    ink_share: float = 0.0
    panels: list[Panel] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    bands: list[TypeBand] = field(default_factory=list)
    palette: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "page": self.index,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "margins_mm": {
                "top": round(self.margin_top_mm, 1),
                "bottom": round(self.margin_bottom_mm, 1),
                "left": round(self.margin_left_mm, 1),
                "right": round(self.margin_right_mm, 1),
            },
            "columns": self.columns,
            "gutter_mm": round(self.gutter_mm, 1),
            "column_width_mm": round(self.column_width_mm, 1),
            "paper": self.paper,
            "ink_share": round(self.ink_share, 3),
            "palette": self.palette,
            "panels": [panel.to_dict() for panel in self.panels],
            "rules": [rule.to_dict() for rule in self.rules],
            "type_sizes_pt": sorted({round(band.size_pt, 1) for band in self.bands}),
            "notes": self.notes,
        }


# ------------------------------------------------------------- profiles ----


def _hex(pixel: tuple[int, ...]) -> str:
    """A colour written the way a designer writes one."""
    return "#{:02x}{:02x}{:02x}".format(*(int(band) for band in pixel[:3]))


def paper_colour(image: Image.Image) -> tuple[int, int, int]:
    """The colour of the paper, taken as the most common light tone.

    Newsprint is not white and a magazine's stock is not either, so every
    other measurement is made relative to whatever this page is printed on.
    """
    small = image.convert("RGB").resize((120, 160), Image.Resampling.BOX)
    counts: dict[tuple[int, int, int], int] = {}
    for pixel in small.getdata():
        key = (pixel[0] // 12 * 12, pixel[1] // 12 * 12, pixel[2] // 12 * 12)
        counts[key] = counts.get(key, 0) + 1
    small.close()
    if not counts:
        return (255, 255, 255)
    # The most common tone that is not dark: a page of solid colour would
    # otherwise report its ink as its paper.
    light = {key: value for key, value in counts.items() if sum(key) / 3 >= 120}
    chosen = max(light or counts, key=lambda key: (light or counts)[key])
    return tuple(min(255, band + 6) for band in chosen)  # type: ignore[return-value]


def ink_mask(image: Image.Image, paper: tuple[int, int, int], margin: int = INK_MARGIN) -> Image.Image:
    """White where the page is inked, black where it is bare.

    Distance from the paper colour rather than plain darkness, so a pale tint
    panel counts as something printed and a dark photograph does not swamp
    the page's own geometry.
    """
    from PIL import ImageChops

    flat = image.convert("RGB")
    difference = ImageChops.difference(flat, Image.new("RGB", flat.size, paper))
    grey = difference.convert("L")
    return grey.point(lambda value: 255 if value > margin else 0)


def row_profile(mask: Image.Image) -> list[float]:
    """Mean ink per row, as a fraction."""
    strip = mask.resize((1, mask.height), Image.Resampling.BOX)
    values = [value / 255.0 for value in strip.getdata()]
    strip.close()
    return values


def column_profile(mask: Image.Image) -> list[float]:
    """Mean ink per column, as a fraction."""
    strip = mask.resize((mask.width, 1), Image.Resampling.BOX)
    values = [value / 255.0 for value in strip.getdata()]
    strip.close()
    return values


def _runs(values: list[float], *, empty: bool, threshold: float) -> list[tuple[int, int]]:
    """Stretches of the profile that are (or are not) below *threshold*."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(values):
        matches = (value <= threshold) if empty else (value > threshold)
        if matches and start is None:
            start = index
        elif not matches and start is not None:
            out.append((start, index))
            start = None
    if start is not None:
        out.append((start, len(values)))
    return out


# -------------------------------------------------------------- margins ----


#: A band of ink this deep, with this much of it inked, is a line of type
#: rather than a photograph (which inks its whole band) or a speck.
TEXT_BAND_MM = (1.0, 34.0)
TEXT_BAND_INK = (0.04, 0.82)


def detect_margins(page: PageRaster, mask: Image.Image) -> tuple[float, float, float, float]:
    """Where the *type* starts and stops, which is where the margins are.

    Not simply where the ink starts: a magazine page with a circle bleeding
    off the trim has ink in its corner and a margin of nothing, which is true
    of that page and useless as a specification. The margin that matters is
    the one the text block is set to, so the text block is what is measured,
    and the ink bounding box is the fallback for a page with no type on it.
    """
    rows = row_profile(mask)
    columns = column_profile(mask)
    inked_rows = [index for index, value in enumerate(rows) if value > EMPTY_SHARE]
    inked_columns = [index for index, value in enumerate(columns) if value > EMPTY_SHARE]
    if not inked_rows or not inked_columns:
        return (0.0, 0.0, 0.0, 0.0)
    fallback = (
        max(0.0, page.mm(inked_rows[0])),
        max(0.0, page.mm(len(rows) - 1 - inked_rows[-1])),
        max(0.0, page.mm(inked_columns[0])),
        max(0.0, page.mm(len(columns) - 1 - inked_columns[-1])),
    )

    text = _text_rows(page, rows)
    if len(text) < 4:
        return fallback
    first, last = text[0][0], text[-1][1]
    strip = mask.crop((0, first, mask.width, last))
    text_columns = column_profile(strip)
    strip.close()
    inked = [index for index, value in enumerate(text_columns) if value > EMPTY_SHARE]
    if not inked:
        return fallback
    return (
        max(0.0, page.mm(first)),
        max(0.0, page.mm(len(rows) - 1 - last)),
        max(0.0, page.mm(inked[0])),
        max(0.0, page.mm(len(text_columns) - 1 - inked[-1])),
    )


def _text_rows(page: PageRaster, rows: list[float]) -> list[tuple[int, int]]:
    """The row bands that look like lines of type rather than pictures."""
    out: list[tuple[int, int]] = []
    for start, finish in _runs(rows, empty=False, threshold=EMPTY_SHARE * 2):
        depth = page.mm(finish - start)
        if not TEXT_BAND_MM[0] <= depth <= TEXT_BAND_MM[1]:
            continue
        ink = sum(rows[start:finish]) / max(1, finish - start)
        if not TEXT_BAND_INK[0] <= ink <= TEXT_BAND_INK[1]:
            continue
        out.append((start, finish))
    return out


# -------------------------------------------------------------- columns ----


#: A gutter is bare down the whole page, not on average. This is the share of
#: the live height that has to be clear before a run of pale columns is one.
GUTTER_CLEAR = 0.94

#: Nobody sets a column narrower than this, so a "grid" that implies one is a
#: misreading rather than a discovery.
MIN_COLUMN_MM = 16.0
MAX_GUTTER_MM = 14.0


#: Column counts worth testing. Below two there is no grid; above this a
#: broadsheet is being read as something it is not.
GRID_CANDIDATES = range(2, 15)

#: Gutter widths worth testing, in millimetres. The fit only needs to land
#: inside the real gutter; its width is measured afterwards.
GUTTER_CANDIDATES = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)

#: Wider than this is a column of white space, not a gutter.

#: Nobody sets a column narrower than this, so a "grid" that implies one is a
#: misreading rather than a discovery.
MIN_COLUMN_MM = 16.0

#: A fit has to be at least this much better than no grid at all.
MIN_GRID_SCORE = 0.08


def detect_columns(
    page: PageRaster,
    mask: Image.Image,
    left_mm: float,
    right_mm: float,
    top_mm: float = 0.0,
    bottom_mm: float = 0.0,
) -> tuple[int, float, float, list[str]]:
    """The column grid, fitted rather than counted.

    Counting gutters does not work on a real newspaper page: a picture across
    four columns, a headline across three and a panel across two break most of
    the gutters on the page, and the two or three that survive are wherever
    the day's pictures happened not to go. Reading the whitespace directly
    gives a different answer on every page of the same paper.

    So the grid is *fitted*. For every plausible column count and gutter
    width, the positions the gutters would fall at are worked out and the page
    is asked how bare it is there and how inked it is between them. The grid
    that explains the page best wins. A picture across four columns costs that
    fit a little; it does not destroy it.
    """
    left = max(0, int(page.px(left_mm)))
    right = max(left + 1, mask.width - int(page.px(right_mm)))
    top = max(0, int(page.px(top_mm)))
    bottom = max(top + 1, mask.height - int(page.px(bottom_mm)))
    live_width_mm = page.mm(right - left)
    if right - left < 60 or bottom - top < 60:
        return (1, 0.0, live_width_mm, ["The live area is too small to read a grid"])

    live = mask.crop((left, top, right, bottom))
    ink = column_profile(live)
    live.close()
    if not ink or max(ink) <= 0:
        return (1, 0.0, live_width_mm, ["The live area is blank"])

    px_per_mm = len(ink) / max(1e-6, live_width_mm)
    best: tuple[float, int, float] = (0.0, 1, 0.0)
    for count in GRID_CANDIDATES:
        for gutter_mm in GUTTER_CANDIDATES:
            column_mm = (live_width_mm - gutter_mm * (count - 1)) / count
            if column_mm < MIN_COLUMN_MM:
                continue
            score = _grid_score(ink, px_per_mm, count, column_mm, gutter_mm)
            if score > best[0]:
                best = (score, count, gutter_mm)

    score, count, gutter_mm = best
    if count > 1:
        measured = _measure_gutter(ink, px_per_mm, count,
                                   (live_width_mm - gutter_mm * (count - 1)) / count, gutter_mm)
        if 0.8 <= measured <= MAX_GUTTER_MM:
            gutter_mm = round(measured, 1)
    if count <= 1 or score < MIN_GRID_SCORE:
        return (
            1,
            0.0,
            live_width_mm,
            ["No column grid fits this page; it is set to a single measure"],
        )
    column_width = (live_width_mm - gutter_mm * (count - 1)) / count
    notes: list[str] = []
    if score < MIN_GRID_SCORE * 2:
        notes.append(
            f"The {count}-column grid is a weak fit on this page; a picture or a panel may "
            "be covering most of it"
        )
    return (count, round(gutter_mm, 1), round(column_width, 1), notes)


#: How much barer than its neighbours a gutter has to be before it counts as
#: one that is really there.
GUTTER_CONFIRMED = 0.35

#: A column of the fitted grid with less than this share of the average ink is
#: not a column at all - it is a gutter the grid has mistaken for one.
BARE_COLUMN = 0.3

#: What such a column costs the fit. This is what stops a grid of twice as
#: many columns scoring as well as the right one: half of its "columns" land
#: on real gutters and are bare.
BARE_COLUMN_COST = 1.0


def _grid_score(
    ink: list[float], px_per_mm: float, count: int, column_mm: float, gutter_mm: float
) -> float:
    """How well one candidate grid explains a page's ink.

    Every gutter the grid predicts is checked separately and the confirmed
    ones are *added up* rather than averaged. Averaging makes a two-column
    grid score as well as a six-column one on a six-column page - its single
    gutter falls in the middle of the page, where there is a real gutter - so
    the page would be read as half the columns it is set in. Counting the
    evidence instead means the six-column grid confirms five gutters and the
    two-column grid confirms one.

    A grid of *twice* as many columns is the case that needs care. Every
    second gutter it predicts falls on a real one, so it confirms as many as
    the right grid does, and its columns are half-columns of real type so none
    of them is bare. What gives it away is the other half: of eleven gutters
    it predicts, six fall down the middle of a column and are not there at
    all. So the evidence is weighed by the *share* of predictions that held,
    which a picture covering four gutters barely dents and a doubled grid
    halves.
    """
    columns: list[float] = []
    for index in range(count):
        start_mm = index * (column_mm + gutter_mm)
        columns.append(_window(ink, px_per_mm, start_mm + column_mm * 0.5, column_mm * 0.6))
    column_ink = sum(columns) / max(1, len(columns))
    if column_ink <= 0:
        return 0.0

    predicted = count - 1
    if predicted <= 0:
        return 0.0
    evidence = 0.0
    confirmed = 0
    for index in range(predicted):
        start_mm = index * (column_mm + gutter_mm)
        gutter_ink = _window(ink, px_per_mm, start_mm + column_mm + gutter_mm * 0.5, gutter_mm * 0.6)
        neighbours = (columns[index] + columns[index + 1]) / 2 or column_ink
        contrast = max(0.0, (neighbours - gutter_ink) / max(1e-6, neighbours))
        if contrast >= GUTTER_CONFIRMED:
            evidence += contrast
            confirmed += 1

    bare = sum(1 for value in columns if value < column_ink * BARE_COLUMN)
    evidence -= BARE_COLUMN_COST * bare
    held = (confirmed / predicted) ** 2
    # Scaled by how much is actually printed, so a nearly blank page does not
    # fit every grid equally well.
    return max(0.0, evidence) * held * min(1.0, column_ink * 6)


def _measure_gutter(
    ink: list[float], px_per_mm: float, count: int, column_mm: float, gutter_mm: float
) -> float:
    """How wide the gutters of a fitted grid actually are.

    The fit only has to find the gutters; their width is then read off the
    page. Trying widths and keeping the best-scoring one reports the narrowest
    that still lands inside the real gutter, which is not the same thing.
    """
    threshold = max(EMPTY_SHARE, sum(ink) / max(1, len(ink)) * 0.18)
    widths: list[float] = []
    for index in range(count - 1):
        centre_mm = index * (column_mm + gutter_mm) + column_mm + gutter_mm * 0.5
        centre = int(centre_mm * px_per_mm)
        if not 0 <= centre < len(ink) or ink[centre] > threshold:
            continue
        left = centre
        while left > 0 and ink[left - 1] <= threshold:
            left -= 1
        right = centre
        while right < len(ink) - 1 and ink[right + 1] <= threshold:
            right += 1
        widths.append((right - left + 1) / px_per_mm)
    if not widths:
        return gutter_mm
    # The narrowest gutter, not the typical one: a gutter is where two columns
    # come closest, and a line that stopped short of its measure widens the
    # white beside it without widening the gutter.
    widths.sort()
    return widths[min(len(widths) - 1, len(widths) // 5)]


def _window(ink: list[float], px_per_mm: float, centre_mm: float, width_mm: float) -> float:
    """Mean ink over a strip of the page, given in millimetres."""
    half = max(1, int(width_mm * px_per_mm / 2))
    centre = int(centre_mm * px_per_mm)
    start = max(0, centre - half)
    finish = min(len(ink), centre + half + 1)
    if finish <= start:
        return 0.0
    return sum(ink[start:finish]) / (finish - start)


# ---------------------------------------------------------------- rules ----


def detect_rules(page: PageRaster, mask: Image.Image, image: Image.Image) -> list[Rule]:
    """The printed lines a page is divided by.

    Two things make a rule. It is **long**: a mark a centimetre across is a
    word, not a rule, and Persian is a connected script so words are long. And
    it is **thin for its length**: a hairline sixty millimetres long is two
    hundred times its own thickness, where a line of type is twenty.

    Both need the thickness measured over the run's *own* span. Asking instead
    whether the next row happens to contain a long run somewhere gives a rule
    the thickness of whatever else is on the page, and a band of body copy
    then reads as a hairline on its last row - which is what made an earlier
    version of this need a third test, that a rule has paper above and below
    it. That test also threw away every box drawn over a column of type, so
    measuring properly is worth more than a rule of thumb.
    """
    out: list[Rule] = []
    max_thickness = max(1, int(page.px(MAX_RULE_MM)) + 1)
    min_length = page.px(MIN_RULE_MM)

    for orientation in ("horizontal", "vertical"):
        working = mask if orientation == "horizontal" else mask.transpose(Image.Transpose.ROTATE_90)
        width, height = working.size
        pixels = working.load()
        claimed: list[tuple[int, int, int, int]] = []
        for row in range(height):
            for begin, finish in _spans(pixels, width, row, min_length):
                if any(
                    top <= row <= bottom and begin < right and finish > left
                    for left, top, right, bottom in claimed
                ):
                    continue
                thickness = _thickness(pixels, height, begin, finish, row, max_thickness)
                # Claimed whether or not it turns out to be a rule: a band of
                # ink that was rejected for being too thick must not be
                # examined again one row lower, where its last row alone looks
                # exactly like a hairline.
                claimed.append((begin, row, finish, row + thickness))
                if thickness > max_thickness:
                    continue
                if (finish - begin) / max(1, thickness) < MIN_RULE_ASPECT:
                    continue
                length = page.mm(finish - begin)
                weight = page.mm(thickness) / 25.4 * 72.0
                colour = _sample(image, working, begin, finish, row, thickness, orientation)
                if orientation == "horizontal":
                    out.append(
                        Rule(page.mm(begin), page.mm(row), length, weight, orientation, colour)
                    )
                else:
                    out.append(
                        Rule(
                            page.mm(height - row - thickness),
                            page.mm(begin),
                            length,
                            weight,
                            orientation,
                            colour,
                        )
                    )
        if working is not mask:
            working.close()
    return out


def _thickness(
    pixels: Any, height: int, begin: int, finish: int, row: int, ceiling: int
) -> int:
    """How deep a run of ink is, measured over that run's own span.

    Stops one row past the ceiling so a thick band can be recognised as too
    thick to be a rule rather than reported as exactly the maximum.
    """
    step = max(1, (finish - begin) // 24)
    samples = max(1, len(range(begin, finish, step)))

    def covered(at: int) -> bool:
        if at < 0 or at >= height:
            return False
        inked = sum(1 for x in range(begin, finish, step) if pixels[x, at])
        return inked / samples >= 0.9

    depth = 0
    while depth <= ceiling and covered(row + depth):
        depth += 1
    return max(1, depth)


def _spans(pixels: Any, width: int, row: int, min_length: float) -> list[tuple[int, int]]:
    """Unbroken inked stretches of one row, long enough to be a rule."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width):
        if pixels[x, row]:
            if start is None:
                start = x
        elif start is not None:
            if x - start >= min_length:
                out.append((start, x))
            start = None
    if start is not None and width - start >= min_length:
        out.append((start, width))
    return out


def _sample(
    image: Image.Image,
    working: Image.Image,
    begin: int,
    finish: int,
    row: int,
    thickness: int,
    orientation: str,
) -> str:
    """The colour a rule is actually printed in."""
    middle = (begin + finish) // 2
    if orientation == "horizontal":
        point = (middle, min(image.height - 1, row + thickness // 2))
    else:
        point = (min(image.width - 1, working.height - row - thickness // 2 - 1), middle)
    try:
        return _hex(image.convert("RGB").getpixel(point))
    except Exception:  # noqa: BLE001 - an edge pixel is not worth failing over
        return "#111111"


# --------------------------------------------------------------- panels ----

#: The page is labelled at this width, whatever it was rendered at. Union-find
#: over a broadsheet's own pixels is a million cells; over this it is forty
#: thousand, which is the difference between a page taking a minute and a
#: second, and a panel is centimetres across so nothing is lost.
LABEL_WIDTH = 300

#: How coarsely colours are grouped before regions are found. A tint panel is
#: rarely one exact value across its whole area - it is screened, or scanned,
#: or saved as JPEG - so bands this wide keep it in one piece.
QUANTISE = 30


def detect_panels(page: PageRaster, image: Image.Image, paper: tuple[int, int, int]) -> list[Panel]:
    """The boxes a page is built from.

    Two kinds, because a designer draws two kinds: a **tint panel**, a region
    of flat colour that is not the paper, and a **keyline box**, whose edge is
    ruled and whose middle is bare. Both are found the same way - regions of
    one colour, kept when they fill their own bounding box - and told apart by
    what is inside them.
    """
    scale = LABEL_WIDTH / max(1, image.width)
    if scale >= 1.0:
        scale = 1.0
    small = image.convert("RGB").resize(
        (max(8, int(image.width * scale)), max(8, int(image.height * scale))),
        Image.Resampling.BOX,
    )
    # A median pass so a halftone photograph reads as one region rather than
    # as ten thousand, which is what makes the labelling cheap and stable.
    small = small.filter(ImageFilter.MedianFilter(3))
    width, height = small.size
    pixels = small.load()

    quantised = [
        [
            (
                pixels[x, y][0] // QUANTISE,
                pixels[x, y][1] // QUANTISE,
                pixels[x, y][2] // QUANTISE,
            )
            for x in range(width)
        ]
        for y in range(height)
    ]
    paper_key = tuple(band // QUANTISE for band in paper)
    regions = _label(quantised, width, height)

    px_per_mm_small = page.px_per_mm * (width / max(1, image.width))
    minimum_cells = max(9, int((MIN_PANEL_MM * px_per_mm_small) ** 2 * 0.25))
    out: list[Panel] = []
    for key, cells in regions:
        if len(cells) < minimum_cells:
            continue
        if key == paper_key and _touches_edge(cells, width, height):
            # The sheet itself. A region of paper that does *not* reach the
            # edge is enclosed by something, and a rectangle of paper with a
            # ruled edge round it is precisely a keyline box.
            continue
        xs = [cell[0] for cell in cells]
        ys = [cell[1] for cell in cells]
        left, right = min(xs), max(xs) + 1
        top, bottom = min(ys), max(ys) + 1
        box_width_mm = (right - left) / px_per_mm_small
        box_height_mm = (bottom - top) / px_per_mm_small
        if box_width_mm < MIN_PANEL_MM or box_height_mm < MIN_PANEL_MM:
            continue
        coverage = len(cells) / max(1, (right - left) * (bottom - top))
        if coverage < PANEL_FILL:
            continue
        colour = _hex(tuple(min(255, band * QUANTISE + QUANTISE // 2) for band in key))
        out.append(
            Panel(
                x_mm=left / px_per_mm_small,
                y_mm=top / px_per_mm_small,
                width_mm=box_width_mm,
                height_mm=box_height_mm,
                color=colour,
                coverage=coverage,
            )
        )
    small.close()

    out = _drop_the_sheet(out, page)
    out = _merge_nested(out)
    _find_keylines(page, image, paper, out)
    out.sort(key=lambda panel: panel.area_mm2, reverse=True)
    return out[:40]


def _label(
    grid: list[list[tuple[int, int, int]]], width: int, height: int
) -> list[tuple[tuple[int, int, int], list[tuple[int, int]]]]:
    """Group cells of one colour, four-connected.

    Returns a list rather than a dictionary keyed by colour, because two
    separate boxes of the same tint are two boxes: keying by colour merges
    them into one bounding box spanning the pair, which is a box that is not
    on the page.

    Union-find with an explicit parent array rather than a recursive flood
    fill: the largest region of a newspaper page is most of the page, and
    Python's stack is not deep enough for that.
    """
    parent = list(range(width * height))

    def find(item: int) -> int:
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for y in range(height):
        row = grid[y]
        previous = grid[y - 1] if y else None
        for x in range(width):
            index = y * width + x
            colour = row[x]
            if x and row[x - 1] == colour:
                union(index - 1, index)
            if previous is not None and previous[x] == colour:
                union(index - width, index)

    by_root: dict[int, list[tuple[int, int]]] = {}
    for y in range(height):
        for x in range(width):
            by_root.setdefault(find(y * width + x), []).append((x, y))

    out: list[tuple[tuple[int, int, int], list[tuple[int, int]]]] = []
    for cells in by_root.values():
        x, y = cells[0]
        out.append((grid[y][x], cells))
    return out


def _overlaps(left: Panel, right: Panel, share: float = 0.5) -> bool:
    """Whether two measured boxes are largely the same box."""
    across = min(left.x_mm + left.width_mm, right.x_mm + right.width_mm) - max(
        left.x_mm, right.x_mm
    )
    down = min(left.y_mm + left.height_mm, right.y_mm + right.height_mm) - max(
        left.y_mm, right.y_mm
    )
    if across <= 0 or down <= 0:
        return False
    return (across * down) / max(1e-6, min(left.area_mm2, right.area_mm2)) >= share


def _touches_edge(cells: list[tuple[int, int]], width: int, height: int) -> bool:
    """Whether a region reaches the edge of the page."""
    return any(
        x == 0 or y == 0 or x == width - 1 or y == height - 1 for x, y in cells
    )


def _drop_the_sheet(panels: list[Panel], page: PageRaster) -> list[Panel]:
    """Remove the region that is simply the page itself."""
    return [
        panel
        for panel in panels
        if not (
            panel.width_mm > page.width_mm * 0.94 and panel.height_mm > page.height_mm * 0.94
        )
    ]


def _merge_nested(panels: list[Panel]) -> list[Panel]:
    """Drop a panel that is entirely inside another of a similar colour."""
    out: list[Panel] = []
    for panel in sorted(panels, key=lambda item: item.area_mm2, reverse=True):
        swallowed = False
        for kept in out:
            inside = (
                panel.x_mm >= kept.x_mm - 1
                and panel.y_mm >= kept.y_mm - 1
                and panel.x_mm + panel.width_mm <= kept.x_mm + kept.width_mm + 1
                and panel.y_mm + panel.height_mm <= kept.y_mm + kept.height_mm + 1
            )
            if inside and _close(panel.color, kept.color, 40):
                swallowed = True
                break
        if not swallowed:
            out.append(panel)
    return out


def _close(left: str, right: str, tolerance: int) -> bool:
    """Whether two colours are near enough to be the same ink."""
    try:
        first = tuple(int(left[index : index + 2], 16) for index in (1, 3, 5))
        second = tuple(int(right[index : index + 2], 16) for index in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=True))


#: How far apart two rules may be and still be the two sides of one box.
BOX_TOLERANCE_MM = 3.0


def boxes_from_rules(
    rules: list[Rule],
    *,
    min_mm: float = MIN_PANEL_MM,
    page: PageRaster | None = None,
    image: Image.Image | None = None,
    paper: tuple[int, int, int] | None = None,
) -> list[Panel]:
    """The ruled boxes a page draws, found as closed rectangles of rules.

    Labelling regions of colour cannot find these: a keyline is often a third
    of a millimetre, which is thinner than the resolution the page is labelled
    at, so the inside of the box joins the paper around it and the box
    disappears. The rules themselves are measured at full resolution, and four
    of them meeting at the corners is exactly what a ruled box is.
    """
    horizontal = [rule for rule in rules if rule.orientation == "horizontal"]
    vertical = [rule for rule in rules if rule.orientation == "vertical"]
    out: list[Panel] = []
    used: set[int] = set()

    for top_index, top in enumerate(horizontal):
        for bottom_index, bottom in enumerate(horizontal):
            if bottom_index <= top_index or bottom_index in used or top_index in used:
                continue
            if abs(top.x_mm - bottom.x_mm) > BOX_TOLERANCE_MM:
                continue
            if abs(top.length_mm - bottom.length_mm) > BOX_TOLERANCE_MM:
                continue
            height = bottom.y_mm - top.y_mm
            if height < min_mm or top.length_mm < min_mm:
                continue
            left = _side(vertical, top.x_mm, top.y_mm, height)
            right = _side(vertical, top.x_mm + top.length_mm, top.y_mm, height)
            if left is None or right is None:
                continue
            used.update({top_index, bottom_index})
            inside = _inside_a_box(page, image, paper, top.x_mm, top.y_mm, top.length_mm, height)
            out.append(
                Panel(
                    x_mm=top.x_mm,
                    y_mm=top.y_mm,
                    width_mm=top.length_mm,
                    height_mm=height,
                    color=inside["color"],
                    keyline=True,
                    keyline_pt=round(max(top.weight_pt, bottom.weight_pt), 2),
                    keyline_color=top.color,
                    coverage=1.0,
                    holds_a_picture=inside["picture"],
                )
            )
            break
    return out


def _inside_a_box(
    page: PageRaster | None,
    image: Image.Image | None,
    paper: tuple[int, int, int] | None,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
) -> dict[str, Any]:
    """What a ruled box has in it: paper, a flat tint, or a photograph.

    They are three different pieces of furniture. A border round a photograph
    is not the box a fact list sits in, and drawing one where the other
    belongs is the sort of thing nobody can name but everybody sees.
    """
    if page is None or image is None or paper is None:
        return {"color": _hex(paper or (255, 255, 255)), "picture": False}
    flat = image.convert("RGB")
    left = max(0, int(page.px(x_mm)) + 2)
    top = max(0, int(page.px(y_mm)) + 2)
    right = min(flat.width, int(page.px(x_mm + width_mm)) - 2)
    bottom = min(flat.height, int(page.px(y_mm + height_mm)) - 2)
    if right - left < 4 or bottom - top < 4:
        return {"color": _hex(paper), "picture": False}
    inside = flat.crop((left, top, right, bottom))
    small = inside.resize((24, 24), Image.Resampling.BOX)
    pixels = list(small.getdata())
    small.close()
    inside.close()
    average = tuple(sum(pixel[band] for pixel in pixels) // len(pixels) for band in range(3))
    spread = sum(
        max(abs(pixel[band] - average[band]) for band in range(3)) for pixel in pixels
    ) / len(pixels)
    return {"color": _hex(average), "picture": spread > 30.0}


def _side(vertical: list[Rule], x_mm: float, y_mm: float, height_mm: float) -> Rule | None:
    """A vertical rule that could be one side of a box."""
    for rule in vertical:
        if abs(rule.x_mm - x_mm) > BOX_TOLERANCE_MM:
            continue
        if abs(rule.y_mm - y_mm) > BOX_TOLERANCE_MM:
            continue
        if abs(rule.length_mm - height_mm) > BOX_TOLERANCE_MM * 2:
            continue
        return rule
    return None


def _find_keylines(
    page: PageRaster, image: Image.Image, paper: tuple[int, int, int], panels: list[Panel]
) -> None:
    """Decide which panels are ruled boxes rather than filled ones.

    A keyline box reads as paper inside and ink on the edge; a tint panel
    reads as its own colour throughout. The distinction matters because the
    two are drawn differently and a page that mixes them up looks wrong in a
    way nobody can name.
    """
    flat = image.convert("RGB")
    for panel in panels:
        if not _close(panel.color, _hex(paper), 26):
            continue
        left = int(page.px(panel.x_mm))
        top = int(page.px(panel.y_mm))
        right = min(flat.width - 1, int(page.px(panel.x_mm + panel.width_mm)))
        bottom = min(flat.height - 1, int(page.px(panel.y_mm + panel.height_mm)))
        if right - left < 4 or bottom - top < 4:
            continue
        edge = _edge_ink(flat, paper, left, top, right, bottom)
        if edge["share"] < 0.55:
            continue
        panel.keyline = True
        panel.keyline_pt = max(0.25, page.mm(edge["thickness"]) / 25.4 * 72.0)
        panel.keyline_color = edge["color"]
        panel.corner_mm = round(page.mm(edge["corner"]), 1)


def _edge_ink(
    image: Image.Image,
    paper: tuple[int, int, int],
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> dict[str, Any]:
    """How much of a box's border is inked, how thick it is, and what colour."""
    pixels = image.load()
    samples: list[tuple[int, int]] = []
    step = max(1, (right - left) // 40)
    for x in range(left, right, step):
        samples.append((x, top))
        samples.append((x, bottom))
    step = max(1, (bottom - top) // 40)
    for y in range(top, bottom, step):
        samples.append((left, y))
        samples.append((right, y))

    inked = [point for point in samples if _distance(pixels[point], paper) > INK_MARGIN]
    share = len(inked) / max(1, len(samples))
    colour = _hex(pixels[inked[len(inked) // 2]]) if inked else "#111111"

    thickness = 1
    middle = (top + bottom) // 2
    while thickness < 12 and left + thickness < right:
        if _distance(pixels[left + thickness, middle], paper) <= INK_MARGIN:
            break
        thickness += 1

    # A rounded corner reads as paper where a square one reads as ink.
    corner = 0
    while corner < 30 and left + corner < right and top + corner < bottom:
        if _distance(pixels[left + corner, top + corner], paper) > INK_MARGIN:
            break
        corner += 1
    return {"share": share, "thickness": thickness, "color": colour, "corner": corner}


def _distance(pixel: tuple[int, ...], reference: tuple[int, int, int]) -> int:
    """How far a pixel is from a colour, on its furthest channel."""
    return max(abs(int(pixel[band]) - reference[band]) for band in range(3))


# ----------------------------------------------------------------- type ----

#: The band a line of type inks - ascender to descender - is about this share
#: of the point size, for a Persian face and near enough for a Latin one. The
#: size is the band divided by it, so this is a fraction rather than a
#: multiplier; getting that the wrong way round reports nine-point body copy
#: as five-point. The result is an estimate off pixels, not the paper's own
#: specification, and is reported as one.
INK_BAND_RATIO = 0.72

MIN_BAND_MM = 1.0
MAX_BAND_MM = 60.0


def measure_type(
    page: PageRaster,
    mask: Image.Image,
    *,
    left_mm: float,
    right_mm: float,
    top_mm: float,
    bottom_mm: float,
    columns: int = 1,
    gutter_mm: float = 0.0,
) -> list[TypeBand]:
    """The lines of type on a page, measured as bands of ink.

    A line of type inks a band of rows and leaves a gap before the next; the
    band's depth gives the size and the distance between two bands gives the
    leading.

    Measured one column at a time, not across the page. A row profile taken
    over the whole live area never falls to zero on a real page - a rule down
    the side, an ornament bleeding off the trim, a picture in the next column
    all keep it inked - so every line of a story merges into one band and a
    magazine reports its body copy as hundred-and-fifty-point type. Inside a
    single column the gaps between the lines are real.
    """
    left = max(0, int(page.px(left_mm)))
    right = min(mask.width, mask.width - int(page.px(right_mm)))
    top = max(0, int(page.px(top_mm)))
    bottom = min(mask.height, mask.height - int(page.px(bottom_mm)))
    if right - left < 8 or bottom - top < 8:
        return []

    live_width_mm = page.mm(right - left)
    if columns > 1:
        column_mm = (live_width_mm - gutter_mm * (columns - 1)) / columns
        strips = [
            (
                left + int(page.px(index * (column_mm + gutter_mm))),
                left + int(page.px(index * (column_mm + gutter_mm) + column_mm)),
            )
            for index in range(columns)
        ]
    else:
        # One measure: the middle of it, away from whatever decorates the edges.
        inset = int(page.px(live_width_mm * 0.15))
        strips = [(left + inset, right - inset)]

    bands: list[TypeBand] = []
    for start_x, finish_x in strips:
        if finish_x - start_x < 8:
            continue
        strip = mask.crop((start_x, top, finish_x, bottom))
        rows = row_profile(strip)
        strip.close()
        bands.extend(_bands_of(page, rows, top_mm))

    bands.sort(key=lambda band: band.y_mm)
    for index, band in enumerate(bands[:-1]):
        gap = bands[index + 1].y_mm - band.y_mm
        # Only a following line of the same column is this one's leading; the
        # first line of the next column is metres away in reading order.
        if 0 < gap <= band.height_mm * 4:
            band.leading_pt = round(gap / 25.4 * 72.0, 1)
    return bands


def _bands_of(page: PageRaster, rows: list[float], top_mm: float) -> list[TypeBand]:
    """The bands of one strip of a page."""
    out: list[TypeBand] = []
    for start, finish in _runs(rows, empty=False, threshold=EMPTY_SHARE * 2):
        depth_mm = page.mm(finish - start)
        if depth_mm < MIN_BAND_MM or depth_mm > MAX_BAND_MM:
            continue
        ink = sum(rows[start:finish]) / max(1, finish - start)
        if not TEXT_BAND_INK[0] <= ink <= TEXT_BAND_INK[1]:
            # A band inked from edge to edge is a photograph, and reporting it
            # as hundred-point type puts a size in the scale nobody sets.
            continue
        out.append(
            TypeBand(
                y_mm=top_mm + page.mm(start),
                height_mm=depth_mm,
                size_pt=depth_mm / 25.4 * 72.0 / INK_BAND_RATIO,
                ink=ink,
            )
        )
    return out


def type_scale(bands: list[TypeBand], *, steps: int = 6) -> list[float]:
    """The sizes this publication actually sets, clustered.

    Every line is measured to a tenth of a point, which is noise: a paper has
    a handful of sizes and sets everything in one of them. Clustering the
    measurements recovers that handful.
    """
    sizes = sorted(band.size_pt for band in bands if 4.0 <= band.size_pt <= 200.0)
    if not sizes:
        return []
    clusters: list[list[float]] = [[sizes[0]]]
    for size in sizes[1:]:
        # Within a tenth of the running mean is the same size set twice.
        current = clusters[-1]
        mean = sum(current) / len(current)
        if abs(size - mean) <= max(0.6, mean * 0.09):
            current.append(size)
        else:
            clusters.append([size])
    ranked = sorted(clusters, key=len, reverse=True)[:steps]
    return sorted({round(sum(group) / len(group), 1) for group in ranked})


# --------------------------------------------------------------- palette ---


def palette(image: Image.Image, count: int = 6) -> list[str]:
    """The colours the publication is printed in."""
    from app.creative.reference import extract_palette

    try:
        swatches = extract_palette(image.convert("RGB"), count=count)
    except Exception as exc:  # noqa: BLE001 - a page is not worth failing over
        log.debug("The palette could not be read: %s", exc)
        return []
    return [swatch.hex for swatch in swatches]


# ------------------------------------------------------------- the page ----


def measure_page(page: PageRaster) -> PageMeasurements:
    """Everything one page has to say about how it was made."""
    image = page.image
    paper = paper_colour(image)
    mask = ink_mask(image, paper)
    try:
        top, bottom, left, right = detect_margins(page, mask)
        columns, gutter, column_width, notes = detect_columns(
            page, mask, left, right, top, bottom
        )
        measurements = PageMeasurements(
            index=page.index,
            width_mm=page.width_mm,
            height_mm=page.height_mm,
            margin_top_mm=top,
            margin_bottom_mm=bottom,
            margin_left_mm=left,
            margin_right_mm=right,
            columns=columns,
            gutter_mm=gutter,
            column_width_mm=column_width,
            paper=_hex(paper),
            ink_share=sum(row_profile(mask)) / max(1, mask.height),
            notes=list(notes),
        )
        measurements.rules = detect_rules(page, mask, image)
        measurements.panels = detect_panels(page, image, paper)
        # A ruled box is four rules, and its keyline is usually finer than the
        # resolution regions are labelled at, so it is found from the rules.
        for box in boxes_from_rules(
            measurements.rules, page=page, image=image, paper=paper
        ):
            if not any(_overlaps(box, existing) for existing in measurements.panels):
                measurements.panels.append(box)
        measurements.panels.sort(key=lambda panel: panel.area_mm2, reverse=True)
        measurements.bands = measure_type(
            page,
            mask,
            left_mm=left,
            right_mm=right,
            top_mm=top,
            bottom_mm=bottom,
            columns=measurements.columns,
            gutter_mm=measurements.gutter_mm,
        )
        measurements.palette = palette(image)
    finally:
        mask.close()
    log.info(
        "Page %d: %.0fx%.0f mm, %d column(s), %d panel(s), %d rule(s)",
        page.index,
        page.width_mm,
        page.height_mm,
        measurements.columns,
        len(measurements.panels),
        len(measurements.rules),
    )
    return measurements
