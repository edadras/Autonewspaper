"""Creative direction: several real concepts, not one safe answer.

An art director who comes back with one idea has not directed anything. What
they bring is three or four, genuinely different from each other, each with a
reason, and they say which one they would run. The client then has a choice
rather than a verdict.

Three colour variations of the same layout is not that. So the concepts here
are built along the axes a designer actually decides on - what carries the
piece, how the type is treated, what the palette does, what the surface is -
and how far apart two concepts are is *measured* rather than asserted. A
proposal that comes back with two concepts too alike is rejected and rebuilt,
which is the only way "show me something different" can be a promise instead
of an intention.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.agents import recipes
from app.agents.recipes import Palette, Recipe, Sheet
from app.core.errors import AppError
from app.creative.style import StyleBrief, contrast_ratio, readable_on, shift

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from app.agents.studio import Brief

log = logging.getLogger(__name__)


class Approach(str, Enum):
    """What carries the piece. This is the first thing a designer decides."""

    PHOTOGRAPHIC = "photographic"
    """The picture is the poster; the type serves it."""
    TYPOGRAPHIC = "typographic"
    """The words are the image. Works with no photograph at all."""
    EDITORIAL = "editorial"
    """A strict grid, a great deal of white, hairlines. The quiet one."""
    GRAPHIC = "graphic"
    """Shape and colour carry it; a picture, if any, is masked into a form."""
    COMPOSITE = "composite"
    """The subject cut out, haloed, with the headline reversed out of a band."""


#: How each approach reads, in the words a director would use to pitch it.
PITCH: dict[Approach, str] = {
    Approach.PHOTOGRAPHIC: (
        "Let the photograph do the work. It fills the sheet, a wash keeps the type "
        "readable over it, and the words stay out of its way."
    ),
    Approach.TYPOGRAPHIC: (
        "Make the words the image. The headline takes the middle of the sheet at the "
        "size the sheet will carry, with a field of colour to anchor the head."
    ),
    Approach.EDITORIAL: (
        "Say it quietly. A strict grid, a great deal of white, two hairlines, and type "
        "at a reading size - it reads as authority rather than as noise."
    ),
    Approach.GRAPHIC: (
        "Build it out of shape and colour. A field cut by a diagonal, a disc the type "
        "overlaps, and the picture masked into the form rather than laid across it."
    ),
    Approach.COMPOSITE: (
        "Cut the subject off its background and set it down over a band, with a halo "
        "in the accent and the headline reversed out so the picture runs through it."
    ),
}

#: Which approaches need a photograph to be worth proposing.
NEEDS_A_PICTURE = {Approach.PHOTOGRAPHIC, Approach.COMPOSITE}

#: How the type is handled. Part of what makes two concepts different.
TREATMENTS: dict[Approach, str] = {
    Approach.PHOTOGRAPHIC: "set over a wash, tight and quiet",
    Approach.TYPOGRAPHIC: "oversized, filling the measure",
    Approach.EDITORIAL: "at a reading size, on a third of the sheet",
    Approach.GRAPHIC: "tracked in tight, sitting inside the shapes",
    Approach.COMPOSITE: "reversed out, the picture running through the letters",
}

#: And the surface each is finished with.
SURFACES: dict[Approach, str] = {
    Approach.PHOTOGRAPHIC: "grain",
    Approach.TYPOGRAPHIC: "risograph",
    Approach.EDITORIAL: "paper",
    Approach.GRAPHIC: "halftone",
    Approach.COMPOSITE: "paper",
}


@dataclass
class Direction:
    """One concept, with the reason it is on the table."""

    name: str
    approach: Approach
    idea: str
    """One line, in the words a director would say it in."""
    rationale: str = ""
    """Why it suits *this* brief rather than any brief."""
    palette: Palette = field(default_factory=Palette)
    treatment: str = ""
    surface: str = ""
    recommended: bool = False
    recipe: Recipe | None = None
    preview: str = ""
    """Where the concept was rendered, once it has been."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "name": self.name,
            "approach": self.approach.value,
            "idea": self.idea,
            "rationale": self.rationale,
            "treatment": self.treatment,
            "surface": self.surface,
            "recommended": self.recommended,
            "recipe": self.recipe.name if self.recipe else "",
            "preview": self.preview,
            "palette": {
                "ground": self.palette.ground,
                "ink": self.palette.ink,
                "accent": self.palette.accent,
                "paper": self.palette.paper,
            },
        }


# ---------------------------------------------------------- distinctness ---

#: Below this two concepts are the same idea twice and one of them is dropped.
#: Not a taste judgement: it is the share of the decisions a designer makes
#: that the two took differently.
MIN_DISTINCTNESS = 0.5


def distinctness(left: Direction, right: Direction) -> float:
    """How far apart two concepts are, from zero to one.

    Weighted by how much each decision changes what the piece looks like. The
    approach dominates, because a photographic poster and an editorial one are
    different pieces of work however they are coloured; the palette counts for
    something; the type treatment and the surface count for a little.
    """
    scores = (
        (0.55, 0.0 if left.approach is right.approach else 1.0),
        (0.25, _palette_distance(left.palette, right.palette)),
        (0.12, 0.0 if left.treatment == right.treatment else 1.0),
        (0.08, 0.0 if left.surface == right.surface else 1.0),
    )
    return round(sum(weight * value for weight, value in scores), 3)


def _palette_distance(left: Palette, right: Palette) -> float:
    """How differently two palettes read, from zero to one."""
    pairs = (
        (left.ground, right.ground),
        (left.ink, right.ink),
        (left.accent, right.accent),
    )
    total = 0.0
    for first, second in pairs:
        total += min(1.0, _colour_gap(first, second) / 160.0)
    return round(total / len(pairs), 3)


def _colour_gap(left: str, right: str) -> float:
    """The distance between two colours, on their furthest channel."""
    try:
        first = [int(left.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4)]
        second = [int(right.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4)]
    except (ValueError, IndexError):
        return 0.0
    return float(max(abs(a - b) for a, b in zip(first, second, strict=True)))


def spread(directions: list[Direction]) -> float:
    """How far apart a whole set is: the closest pair in it.

    The closest pair, not the average: a set of four where two are the same
    idea is a set of three, however different the other two are.
    """
    if len(directions) < 2:
        return 1.0
    return min(
        distinctness(directions[index], directions[other])
        for index in range(len(directions))
        for other in range(index + 1, len(directions))
    )


def prune(directions: list[Direction], *, minimum: float = MIN_DISTINCTNESS) -> list[Direction]:
    """Drop any concept too like one already on the table.

    Order matters: the first of a pair is kept, so a proposal should put the
    strongest idea first.
    """
    kept: list[Direction] = []
    for candidate in directions:
        too_alike = next(
            (item for item in kept if distinctness(item, candidate) < minimum), None
        )
        if too_alike is not None:
            log.info(
                "'%s' is the same idea as '%s' (%.2f apart); dropped",
                candidate.name,
                too_alike.name,
                distinctness(too_alike, candidate),
            )
            continue
        kept.append(candidate)
    return kept


# -------------------------------------------------------------- proposal ---

DIRECTION_PROMPT = """You are an art director presenting concepts.

You do not present one idea. You present {count}, genuinely different from each
other, each with the reason it suits this brief, and you say which you would
run. Three colourways of the same layout is one idea, not three.

Reply with a single JSON object:

{{
  "directions": [
    {{"name": "two or three words",
      "approach": "photographic|typographic|editorial|graphic|composite",
      "idea": "one line, the way you would say it out loud",
      "rationale": "why this suits THIS brief, two sentences",
      "ground": "#0f172a", "ink": "#f8fafc", "accent": "#c2410c",
      "recommended": true}}
  ]
}}

Rules:
- Every approach in the list must be different from the others.
- The palettes must differ too: a set where all three are dark blue with an
  orange accent is one idea in three coats.
- Recommend exactly one, and it does not have to be the safe one.
- photographic and composite need a photograph. If the brief has none, do not
  propose them.
- The rationale names something in this brief. If it would read the same for
  any brief, it is not a rationale.
"""


class CreativeDirection:
    """Proposes several concepts and says which one to run."""

    def __init__(self, ai: Any = None, *, formats: Any = None, dpi: int = 300) -> None:
        self.ai = ai
        self.formats = formats
        self.dpi = dpi

    # ------------------------------------------------------------- entry
    def propose(
        self,
        brief: Brief,
        *,
        style: StyleBrief | None = None,
        count: int = 3,
    ) -> list[Direction]:
        """Come back with *count* concepts that are actually different.

        A model proposes them when one is configured, and what it proposes is
        pruned to what is genuinely distinct and topped up from the router if
        it came back with fewer real ideas than it was asked for. Without a
        model the router does the whole job, which it can, because the axes a
        concept differs on are known rather than imagined.
        """
        wanted = max(1, min(5, count))
        proposed = self._from_model(brief, style, wanted) if self.ai is not None else []
        proposed = prune(proposed)
        if len(proposed) < wanted:
            for candidate in self._from_rules(brief, style, wanted * 2):
                if len(proposed) >= wanted:
                    break
                if all(distinctness(item, candidate) >= MIN_DISTINCTNESS for item in proposed):
                    proposed.append(candidate)
        directions = proposed[:wanted]
        if not any(item.recommended for item in directions) and directions:
            directions[0].recommended = True
        spread_palettes(directions)
        self._attach_recipes(brief, directions)
        log.info(
            "Directed %d concept(s): %s (closest pair %.2f apart)",
            len(directions),
            ", ".join(f"{item.name} [{item.approach.value}]" for item in directions),
            spread(directions),
        )
        return directions

    # ------------------------------------------------------------- model
    def _from_model(
        self, brief: Brief, style: StyleBrief | None, count: int
    ) -> list[Direction]:
        from app.ai.base import ChatMessage, TextRequest

        payload = brief.to_dict()
        if style is not None:
            payload["references_say"] = style.describe()
        request = TextRequest(
            messages=[
                ChatMessage("system", DIRECTION_PROMPT.format(count=count)),
                ChatMessage(
                    "user",
                    f"The brief:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
                    f"Present {count} concepts.",
                ),
            ],
            # Warmer than the rest of the system on purpose: this is the one
            # call where the safe answer is the wrong answer.
            temperature=0.85,
            max_tokens=1600,
            json_mode=True,
            metadata={"task": "creative_direction"},
        )
        try:
            response = self.ai._complete(request, purpose="creative direction")  # noqa: SLF001
            data = response.json(required=False)
        except AppError as exc:
            log.warning("The director's model could not be reached (%s)", exc.message)
            return []
        except Exception as exc:  # noqa: BLE001 - a model failure must not stop the run
            log.warning("Creative direction failed (%s)", exc)
            return []
        return self._parse(data, brief)

    def _parse(self, payload: Any, brief: Brief) -> list[Direction]:
        """Read the concepts out of a model reply, keeping the workable ones."""
        if not isinstance(payload, dict):
            return []
        raw = payload.get("directions")
        if not isinstance(raw, list):
            return []
        out: list[Direction] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            try:
                approach = Approach(str(item.get("approach", "")).strip().lower())
            except ValueError:
                log.info("Dropping a concept with an approach nobody can build: %r", item.get("approach"))
                continue
            if approach in NEEDS_A_PICTURE and not brief.references:
                log.info("'%s' needs a photograph and the brief has none", item.get("name"))
                continue
            palette = self._palette(item)
            out.append(
                Direction(
                    name=str(item.get("name") or f"Concept {index + 1}")[:60],
                    approach=approach,
                    idea=str(item.get("idea") or PITCH[approach])[:300],
                    rationale=str(item.get("rationale") or "")[:600],
                    palette=palette,
                    treatment=TREATMENTS[approach],
                    surface=SURFACES[approach],
                    recommended=bool(item.get("recommended")),
                )
            )
        return out

    def _palette(self, item: dict[str, Any]) -> Palette:
        """The concept's colours, kept readable whatever was suggested."""
        ground = _clean(item.get("ground"), "#0f172a")
        ink = _clean(item.get("ink"), readable_on(ground))
        if contrast_ratio(ground, ink) < 4.5:
            ink = readable_on(ground)
        accent = _clean(item.get("accent"), "#c2410c")
        if contrast_ratio(ground, accent) < 2.0:
            accent = shift(accent, lighten=0.35, saturate=0.2)
        return Palette(ground=ground, ink=ink, accent=accent, paper=shift(ground, lighten=0.92))

    # ------------------------------------------------------------- rules
    def _from_rules(
        self, brief: Brief, style: StyleBrief | None, count: int
    ) -> list[Direction]:
        """Build concepts without a model, along the axes they differ on.

        Not a degraded path. Which approaches suit a brief is a question with
        a known answer - a composite needs a subject to cut out, an editorial
        concept needs copy to set - and the palettes are derived to be
        different from each other rather than sampled and hoped over.
        """
        from app.agents.studio import _split_content

        base = Palette.from_brief(style)
        headline = _split_content(brief)["headline"]
        order = self._approaches_for(brief)
        out: list[Direction] = []
        for index, approach in enumerate(order[:count]):
            palette = _shift_palette(base, approach, index)
            out.append(
                Direction(
                    name=_name_for(approach, brief.language),
                    approach=approach,
                    idea=PITCH[approach],
                    rationale=_rationale_for(approach, brief, headline),
                    palette=palette,
                    treatment=TREATMENTS[approach],
                    surface=SURFACES[approach],
                    recommended=index == 0,
                )
            )
        return out

    def _approaches_for(self, brief: Brief) -> list[Approach]:
        """Which approaches are worth putting on the table for this brief.

        Ordered by how well each suits it, so the first is the one to run.
        """
        has_picture = bool(brief.references)
        subjects = len(brief.references) > 1
        copy = len((brief.content or "").split()) > 40

        order: list[Approach] = []
        if subjects:
            order.append(Approach.COMPOSITE)
        if has_picture:
            order.append(Approach.PHOTOGRAPHIC)
        order.append(Approach.TYPOGRAPHIC)
        order.append(Approach.GRAPHIC)
        if copy or not has_picture:
            order.append(Approach.EDITORIAL)
        if has_picture and Approach.COMPOSITE not in order:
            order.append(Approach.COMPOSITE)
        # Nothing twice, and nothing that needs a picture there is not.
        seen: set[Approach] = set()
        return [
            approach
            for approach in order
            if not (approach in seen or seen.add(approach))
            and (has_picture or approach not in NEEDS_A_PICTURE)
        ]

    # ----------------------------------------------------------- recipes
    def _attach_recipes(self, brief: Brief, directions: list[Direction]) -> None:
        """Give each concept the sequence of calls that builds it."""
        from app.agents.studio import _slug, _split_content

        parts = _split_content(brief)
        stem = _slug(brief.title or brief.request or "concept")
        item = brief.format or "A3"
        rtl = brief.language in ("fa", "ar")
        sheet = self._sheet(item, rtl=rtl)
        photo = brief.references[0] if brief.references else ""

        builders = {
            Approach.PHOTOGRAPHIC: recipes.poster,
            Approach.TYPOGRAPHIC: recipes.poster,
            Approach.EDITORIAL: recipes.editorial_poster,
            Approach.GRAPHIC: recipes.graphic_poster,
        }
        for direction in directions:
            name = f"{stem}_{direction.approach.value}"
            style = _as_brief(direction.palette)
            if direction.approach is Approach.COMPOSITE:
                direction.recipe = recipes.composite_cover(
                    design=name,
                    format=item,
                    sheet=sheet,
                    headline=parts["headline"],
                    kicker=parts["kicker"],
                    detail=parts["detail"],
                    photo=photo,
                    subject=brief.references[1] if len(brief.references) > 1 else photo,
                    brief=style,
                    language=brief.language,
                    texture=direction.surface,
                )
                continue
            build = builders[direction.approach]
            direction.recipe = build(
                design=name,
                format=item,
                sheet=sheet,
                headline=parts["headline"],
                kicker=parts["kicker"],
                detail=parts["detail"],
                # The typographic concept is the one that works with no
                # picture, so it is not given one even when there is one.
                photo="" if direction.approach is Approach.TYPOGRAPHIC else photo,
                brief=style,
                language=brief.language,
            )

    def _sheet(self, item: str, *, rtl: bool) -> Sheet:
        if self.formats is None:
            from app.formats.registry import FormatRegistry

            self.formats = FormatRegistry()
        return Sheet.resolve(self.formats, item, rtl=rtl)


def _clean(value: Any, fallback: str) -> str:
    """A hex colour out of whatever the model sent."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = f"#{text}"
    body = text[1:]
    if len(body) == 3:
        body = "".join(character * 2 for character in body)
    if len(body) != 6 or any(character not in "0123456789abcdefABCDEF" for character in body):
        return fallback
    return f"#{body.lower()}"


def _as_brief(palette: Palette) -> StyleBrief:
    """A concept's palette in the shape the recipes take."""
    return StyleBrief(
        background=palette.ground,
        foreground=palette.ink,
        accent=palette.accent,
    )


#: Where each approach puts its ground on the scale from black to paper.
#: An absolute position rather than a nudge from the brief's own colour: two
#: different nudges downward from an already-dark ground both arrive at black,
#: and two concepts that both come back black have one palette between them
#: however differently they were asked to be shifted.
GROUND_POSITION: dict[Approach, float] = {
    Approach.PHOTOGRAPHIC: 0.08,
    Approach.COMPOSITE: 0.20,
    Approach.TYPOGRAPHIC: 0.34,
    Approach.GRAPHIC: 0.0,
    Approach.EDITORIAL: 0.97,
}

#: How far apart two palettes in one set have to be. Below this the later one
#: is pushed until they read as two palettes.
MIN_PALETTE_GAP = 0.22


def _ground_at(colour: str, position: float) -> str:
    """The colour's hue placed at *position* on the black-to-white scale.

    Mixing rather than lightening, so the destination is where it was asked
    for whatever the starting colour was: a nudge saturates, a mix does not.
    """
    try:
        bands = [int(colour.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4)]
    except (ValueError, IndexError):
        bands = [15, 23, 42]
    share = max(0.0, min(1.0, position))
    # Keep a little of the original hue at both ends, so a set of concepts
    # still looks like one house rather than four unrelated ones.
    if share <= 0.5:
        factor = 0.25 + share * 1.2
        mixed = [int(band * factor) for band in bands]
    else:
        towards = (share - 0.5) * 2
        mixed = [int(band + (255 - band) * (0.35 + towards * 0.62)) for band in bands]
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, band)) for band in mixed))


def _shift_palette(base: Palette, approach: Approach, index: int) -> Palette:
    """The palette this approach works in.

    An editorial concept is set on paper, a photographic one on near-black so
    the picture carries, and a graphic one on a deep version of the accent
    itself - a field of colour rather than a neutral. That is part of what
    makes them different concepts rather than different coats of one.
    """
    if approach is Approach.GRAPHIC:
        # The field *is* the colour, so the ground comes from the accent.
        ground = _ground_at(base.accent, 0.30)
        accent = shift(base.accent, lighten=0.4, saturate=0.25)
    else:
        ground = _ground_at(base.ground, GROUND_POSITION.get(approach, 0.3))
        accent = shift(base.accent, saturate=0.1 * (index + 1), lighten=0.12 * index)

    ground, ink = _readable(ground)
    accent = _keep_accent(accent, ground, ink, fallback=base.accent)
    return Palette(
        ground=ground,
        ink=ink,
        accent=accent,
        paper=_ground_at(base.ground, 0.97),
    )


#: An accent has to be this far from the ink on some channel, or it is not a
#: second colour - it is the type again.
MIN_ACCENT_FROM_INK = 60

#: And this readable against the ground, or nobody sees it at all.
MIN_ACCENT_CONTRAST = 2.4


def _keep_accent(accent: str, ground: str, ink: str, *, fallback: str) -> str:
    """Keep an accent an accent.

    It has to be seen against the ground, and it has to be seen *as a colour*
    beside the ink: an accent lightened until it reads white on a dark ground
    is not a second colour, it is the type again.

    Both constraints pull opposite ways on a dark ground with white type, so
    the hue is walked through a handful of treatments rather than nudged once
    and hoped over; the best of them is taken when none is outright good,
    which is better than handing back one that fails both.
    """
    candidates = [accent]
    for saturate, lighten in (
        (0.0, 0.0),
        (0.35, 0.0),
        (0.45, -0.22),
        (0.45, 0.22),
        (0.2, -0.4),
        (0.2, 0.4),
        (0.6, -0.1),
    ):
        candidates.append(shift(fallback, saturate=saturate, lighten=lighten))

    def score(colour: str) -> tuple[int, float]:
        from_ink = _colour_gap(colour, ink)
        on_ground = contrast_ratio(ground, colour)
        good = int(from_ink >= MIN_ACCENT_FROM_INK and on_ground >= MIN_ACCENT_CONTRAST)
        # How well it does on the constraint it is worse at, so a colour that
        # is superb against one and hopeless against the other does not win.
        return (
            good,
            min(from_ink / MIN_ACCENT_FROM_INK, on_ground / MIN_ACCENT_CONTRAST),
        )

    return max(candidates, key=score)


#: What the ink has to have against its ground. A concept whose headline
#: cannot be read is not a concept, however good the idea behind it is.
MIN_INK_CONTRAST = 4.5


def _readable(ground: str) -> tuple[str, str]:
    """A ground and an ink that can actually be read on it.

    ``readable_on`` picks the better of white and near-black, and on a
    mid-tone ground the better of those is still not good enough. Rather than
    hand back type nobody can read, the *ground* moves: a concept is a set of
    decisions, and "not mid-grey" is one a designer would have made anyway.
    """
    ink = readable_on(ground)
    if contrast_ratio(ground, ink) >= MIN_INK_CONTRAST:
        return (ground, ink)
    # Away from the middle, in whichever direction it is already leaning.
    for step in (0.18, 0.34, 0.5, 0.7):
        for amount in ((-step, step) if _luminance_of(ground) < 0.5 else (step, -step)):
            moved = shift(ground, lighten=amount)
            candidate = readable_on(moved)
            if contrast_ratio(moved, candidate) >= MIN_INK_CONTRAST:
                return (moved, candidate)
    return ("#0f172a", "#ffffff")


def _rotate_hue(colour: str, turns: float) -> str:
    """Move a colour round the wheel, keeping how light and how strong it is.

    When two concepts cannot be pushed apart on lightness - a brief whose own
    colours are already at the end of the scale - the accent can still be a
    different colour, and a different colour is what a second concept needs.
    """
    import colorsys

    try:
        bands = [int(colour.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    except (ValueError, IndexError):
        return colour
    hue, lightness, saturation = colorsys.rgb_to_hls(*bands)
    turned = colorsys.hls_to_rgb((hue + turns) % 1.0, lightness, saturation)
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(band * 255))) for band in turned))


def _luminance_of(colour: str) -> float:
    """How light a colour is, from zero to one."""
    from app.creative.style import relative_luminance

    try:
        return relative_luminance(colour)
    except Exception:  # noqa: BLE001 - a malformed swatch reads as mid
        return 0.5


def spread_palettes(directions: list[Direction], *, minimum: float = MIN_PALETTE_GAP) -> None:
    """Push apart any two palettes in a set that read as the same one.

    The per-approach positions do most of this, but a brief whose own colours
    sit close to one of them can still bring two concepts together. Checked
    and corrected rather than hoped over, because "show me something
    different" has to mean something.
    """
    for index, direction in enumerate(directions):
        for earlier in directions[:index]:
            attempts = 0
            while (
                _palette_distance(earlier.palette, direction.palette) < minimum
                and attempts < 6
            ):
                attempts += 1
                # Away from the one it clashes with, further each time: a
                # fixed step can walk into the end of the scale and stay there.
                lighter = _luminance_of(earlier.palette.ground) < 0.5
                step = 0.2 * attempts
                ground, ink = _readable(
                    shift(direction.palette.ground, lighten=step if lighter else -step)
                )
                if _colour_gap(ground, direction.palette.ground) < 12:
                    # The ground has run into the end of the scale, so the
                    # colours move round the wheel instead of up and down it.
                    ground, ink = _readable(_rotate_hue(direction.palette.ground, 0.17 * attempts))
                direction.palette = Palette(
                    ground=ground,
                    ink=ink,
                    # Pushed, then checked: a push that lightens the accent
                    # until it reads as the type has not made two palettes,
                    # it has taken a colour out of one.
                    accent=_keep_accent(
                        _rotate_hue(direction.palette.accent, 0.12 * attempts),
                        ground,
                        ink,
                        fallback=_rotate_hue(direction.palette.accent, 0.12 * attempts),
                    ),
                    paper=direction.palette.paper,
                )
            if attempts:
                log.info(
                    "'%s' read as the same palette as '%s'; pushed %d step(s) apart",
                    direction.name,
                    earlier.name,
                    attempts,
                )


#: What each concept is called when the router names it. Short, so it fits a
#: card, and in the operator's own language.
_NAMES: dict[str, dict[Approach, str]] = {
    "fa": {
        Approach.PHOTOGRAPHIC: "تصویرمحور",
        Approach.TYPOGRAPHIC: "تایپوگرافیک",
        Approach.EDITORIAL: "نشریه‌ای",
        Approach.GRAPHIC: "گرافیکی",
        Approach.COMPOSITE: "ترکیبی",
    },
    "en": {
        Approach.PHOTOGRAPHIC: "Picture-led",
        Approach.TYPOGRAPHIC: "Type as image",
        Approach.EDITORIAL: "Editorial",
        Approach.GRAPHIC: "Shape and colour",
        Approach.COMPOSITE: "Built cover",
    },
}


def _name_for(approach: Approach, language: str) -> str:
    """The concept's name, in the operator's language."""
    return _NAMES.get(language, _NAMES["en"]).get(approach, approach.value.title())


def _rationale_for(approach: Approach, brief: Brief, headline: str = "") -> str:
    """Why this approach suits this brief, from what the brief actually has.

    Written from the material rather than from a phrase book: a rationale that
    would read the same for any brief is not a rationale.
    """
    pictures = len(brief.references)
    words = len((brief.content or "").split())
    if approach is Approach.COMPOSITE:
        return (
            f"There are {pictures} pictures here, so one can be cut out and set over the "
            "other - which is the only one of these that uses both."
        )
    if approach is Approach.PHOTOGRAPHIC:
        return (
            "There is a photograph, and it is the strongest thing in the brief; this puts "
            "it at the size it deserves and keeps the type out of it."
        )
    if approach is Approach.TYPOGRAPHIC:
        words = len(headline.split())
        if 0 < words <= 7:
            return (
                f"The headline is {words} word(s) - short enough to be the image itself. It "
                "is also the concept that survives the picture falling through."
            )
        return "The words carry this one on their own, so nothing depends on the picture."
    if approach is Approach.EDITORIAL:
        return (
            f"There are {words} words of copy, which is enough to set properly; this gives "
            "them room and lets the white space do the work."
            if words
            else "Nothing here needs to shout, and restraint reads as authority."
        )
    return (
        "Shape and colour carry this one, so it holds up at any size and needs no "
        "photograph at all."
    )


# --------------------------------------------------------- contact sheet ---

#: How wide one concept is drawn on the sheet. Big enough to judge a layout,
#: small enough that four of them fit on a screen at once.
THUMB_WIDTH = 380

#: Room under each concept for its name and the line that pitches it.
CAPTION_HEIGHT = 96


def contact_sheet(
    directions: list[Direction],
    target: Path | str,
    *,
    title: str = "",
    columns: int = 0,
) -> Path:
    """Lay the concepts out side by side, the way they would be pinned up.

    A director presents work, not descriptions. Reading three paragraphs about
    three ideas is not the same as seeing them next to each other, and the
    whole point of proposing more than one is the comparison.
    """
    from PIL import Image, ImageDraw

    from app.vision.fonts import load_font

    drawn = [item for item in directions if item.preview and Path(item.preview).exists()]
    if not drawn:
        raise ValueError("None of these concepts has been rendered yet")

    across = columns or min(4, len(drawn))
    down = (len(drawn) + across - 1) // across
    thumbs: list[Image.Image] = []
    tallest = 0
    for direction in drawn:
        with Image.open(direction.preview) as opened:
            picture = opened.convert("RGB")
        scale = THUMB_WIDTH / max(1, picture.width)
        thumb = picture.resize(
            (THUMB_WIDTH, max(1, int(picture.height * scale))), Image.Resampling.LANCZOS
        )
        picture.close()
        thumbs.append(thumb)
        tallest = max(tallest, thumb.height)

    gap = 28
    header = 64 if title else gap
    sheet = Image.new(
        "RGB",
        (
            across * THUMB_WIDTH + (across + 1) * gap,
            header + down * (tallest + CAPTION_HEIGHT + gap),
        ),
        (250, 250, 249),
    )
    draw = ImageDraw.Draw(sheet)
    heading = load_font("", 26, style="Bold")
    label = load_font("", 21, style="Bold")
    body = load_font("", 16)

    if title:
        draw.text((gap, 22), title, font=heading, fill=(20, 20, 24))

    for index, (direction, thumb) in enumerate(zip(drawn, thumbs, strict=True)):
        column, row = index % across, index // across
        x = gap + column * (THUMB_WIDTH + gap)
        y = header + row * (tallest + CAPTION_HEIGHT + gap)
        sheet.paste(thumb, (x, y + (tallest - thumb.height)))
        thumb.close()

        caption = y + tallest + 12
        mark = "● " if direction.recommended else ""
        draw.text((x, caption), f"{mark}{direction.name}", font=label, fill=(20, 20, 24))
        draw.text(
            (x, caption + 26),
            _wrap(direction.idea, 46),
            font=body,
            fill=(90, 90, 96),
        )
        # The palette, as three chips: the fastest way to see that these are
        # three concepts rather than three coats of one.
        for chip, colour in enumerate(
            (direction.palette.ground, direction.palette.ink, direction.palette.accent)
        ):
            left = x + THUMB_WIDTH - (3 - chip) * 22
            draw.rectangle(
                [left, caption + 2, left + 16, caption + 18],
                fill=colour,
                outline=(210, 210, 208),
            )

    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    sheet.close()
    log.info("Pinned up %d concept(s) -> %s", len(drawn), path)
    return path


def _wrap(text: str, width: int, lines: int = 3) -> str:
    """Break a pitch into a few short lines."""
    words = text.split()
    out: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        out.append(current)
        current = word
        if len(out) >= lines:
            break
    if current and len(out) < lines:
        out.append(current)
    return "\n".join(out)
