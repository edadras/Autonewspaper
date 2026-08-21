"""The studio: an art director and three specialists working at once.

The user hands over their material and their idea. The art director decides
what should be made and in which application - or asks, when the answer
genuinely changes the work - and the specialists build it.

Three things make this a crew rather than one agent with more tools:

* **Each specialist owns one application.** It holds that application's tools
  and no others, so §55's permission check is also a division of labour: a
  Premiere specialist cannot put a layer into somebody's poster, however
  confused it gets.
* **They run at the same time.** Photoshop, InDesign and Premiere are three
  processes; there is no reason to wait for one before starting another. What
  cannot overlap is two agents inside the *same* application, because each has
  one active document - so a host is held for the length of a build, and the
  parallelism is across applications, which is where it actually exists.
* **They hand work to each other.** Anything a specialist finishes is
  published to the blackboard by name, and the assignments that need it are
  told to wait for it. That is how a design drawn in Photoshop ends up placed
  in InDesign and composited over footage in Premiere.

Every specialist is bounded per §57 - iterations, wall clock and retries - and
what it produces is measured before it is handed on.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents import recipes
from app.agents.autonomous import AgentRun, AutonomousAgent
from app.agents.studio_tools import (
    StudioContext,
    build_design_tools,
    build_page_tools,
    build_video_tools,
)
from app.agents.tools import PermissionPolicy, ToolRegistry
from app.core.errors import AppError
from app.core.events import EventBus, EventType
from app.core.jobs import CancelToken
from app.creative.style import StyleBrief

log = logging.getLogger(__name__)

#: Which capabilities each specialist is allowed to hold. This is the same
#: list the tools are registered under, so a specialist that somehow acquired
#: another's tool still could not call it.
CAPABILITIES = {
    "photoshop": {"design.read", "design.write", "design.build", "studio.read", "studio.write"},
    "indesign": {"design.read", "design.write", "design.build", "studio.read", "studio.write"},
    "premiere": {
        "video.read",
        "video.write",
        "video.build",
        "video.generate",
        "studio.read",
        "studio.write",
    },
}

BUILDERS = {
    "photoshop": build_design_tools,
    "indesign": build_page_tools,
    "premiere": build_video_tools,
}

TRADE = {
    "photoshop": "an image maker: posters, covers, composites, the furniture a page is built from",
    "indesign": "a page designer: documents, spreads, columns, the type on the page",
    "premiere": "an editor: sequences, cuts, transitions, titles and graphics over footage",
}


# ------------------------------------------------------------------ brief ---


@dataclass
class Brief:
    """What the user asked for, and what they gave us to do it with."""

    request: str
    """Their own words."""
    content: str = ""
    """The copy, the script, the material."""
    references: list[str] = field(default_factory=list)
    """Images or clips they attached."""
    footage: list[str] = field(default_factory=list)
    """Video or audio to cut."""
    hosts: list[str] = field(default_factory=list)
    """Applications they named. Empty means the director decides."""
    format: str = ""
    """A size they named, if they named one."""
    language: str = "fa"
    title: str = ""
    """A working name for the job."""
    answers: dict[str, str] = field(default_factory=dict)
    """Replies to questions asked on an earlier pass."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "request": self.request,
            "content": self.content[:2000],
            "references": self.references,
            "footage": self.footage,
            "hosts": self.hosts,
            "format": self.format,
            "language": self.language,
            "title": self.title,
            "answers": self.answers,
        }


@dataclass
class Option:
    """One answer on an interactive card."""

    label: str
    detail: str = ""
    recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"label": self.label, "detail": self.detail, "recommended": self.recommended}


@dataclass
class Question:
    """Something the director needs to know before it can do good work.

    A question is only worth asking when the answer changes what gets made.
    Anything the system can decide on the evidence, it decides.
    """

    id: str
    question: str
    why: str = ""
    options: list[Option] = field(default_factory=list)
    multi: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "id": self.id,
            "question": self.question,
            "why": self.why,
            "multi": self.multi,
            "options": [option.to_dict() for option in self.options],
        }


@dataclass
class Assignment:
    """One piece of work, given to one application."""

    id: str
    host: str
    goal: str
    detail: str = ""
    depends_on: list[str] = field(default_factory=list)
    recipe: recipes.Recipe | None = None
    max_iterations: int = 18
    timeout_seconds: float = 900.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "id": self.id,
            "host": self.host,
            "goal": self.goal,
            "detail": self.detail,
            "depends_on": self.depends_on,
            "recipe": self.recipe.name if self.recipe else "",
        }


@dataclass
class Concept:
    """What the director decided to make, and why."""

    name: str = ""
    rationale: str = ""
    assignments: list[Assignment] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    source: str = "rules"
    """``model`` when a model planned it, ``rules`` when the router did."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "name": self.name,
            "rationale": self.rationale,
            "source": self.source,
            "assignments": [item.to_dict() for item in self.assignments],
            "questions": [item.to_dict() for item in self.questions],
        }


@dataclass
class SpecialistResult:
    """What one specialist did."""

    assignment: Assignment
    run: AgentRun | None = None
    error: str = ""
    skipped: str = ""

    @property
    def ok(self) -> bool:
        """Whether the work was done."""
        return not self.error and not self.skipped and bool(self.run and self.run.finished)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "assignment": self.assignment.to_dict(),
            "ok": self.ok,
            "error": self.error,
            "skipped": self.skipped,
            "run": self.run.to_dict() if self.run else None,
        }


@dataclass
class StudioRun:
    """Everything the crew produced."""

    brief: Brief
    concept: Concept = field(default_factory=Concept)
    results: list[SpecialistResult] = field(default_factory=list)
    artefacts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, str]] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    duration: float = 0.0
    stop_reason: str = ""

    @property
    def files(self) -> list[str]:
        """Every file the run produced."""
        return [item["path"] for item in self.artefacts if item.get("path")]

    @property
    def finished(self) -> bool:
        """Whether every assignment was carried out."""
        return bool(self.results) and all(result.ok for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "brief": self.brief.to_dict(),
            "concept": self.concept.to_dict(),
            "finished": self.finished,
            "stop_reason": self.stop_reason,
            "duration": round(self.duration, 2),
            "questions": [item.to_dict() for item in self.questions],
            "results": [item.to_dict() for item in self.results],
            "artefacts": self.artefacts,
            "notes": self.notes,
        }


# --------------------------------------------------------------- director ---

DIRECTOR_PROMPT = """You are the art director of a design studio with three
specialists: one in Adobe Photoshop, one in Adobe InDesign and one in Adobe
Premiere Pro. They work at the same time, in different applications.

You decide what to make and who makes it. You do not make anything yourself.

Reply with a single JSON object:

{
  "concept": "the idea, in one line",
  "rationale": "why this idea suits this brief, in two or three sentences",
  "assignments": [
    {"id": "artwork", "host": "photoshop", "goal": "...", "detail": "...",
     "depends_on": []},
    {"id": "page", "host": "indesign", "goal": "...", "detail": "...",
     "depends_on": ["artwork"]}
  ],
  "questions": [
    {"id": "tone", "question": "...", "why": "...",
     "options": [{"label": "...", "detail": "...", "recommended": true}]}
  ]
}

Rules:
- A host is one of photoshop, indesign, premiere. Nothing else.
- An assignment that needs another's output lists it in depends_on. Anything
  built in Photoshop for a page or a video is a dependency of that page or
  video.
- Assignments with no dependency between them run at the same time; write them
  so they can.
- The goal is what the specialist should achieve, in one sentence. The detail
  carries the material: the words to set, the colours, the sizes, the files.
- Ask a question only when the answer changes what gets made, and never more
  than three. Give real options with a recommendation. If the brief already
  says enough, ask nothing.
- Say what the material supports. Do not invent copy the brief did not give.
"""


class ArtDirector:
    """Turns a brief into assignments, and asks what it needs to know."""

    def __init__(self, ai: Any = None, *, formats: Any = None, dpi: int = 300) -> None:
        self.ai = ai
        self.formats = formats
        self.dpi = dpi

    # ------------------------------------------------------------- entry
    def plan(self, brief: Brief, *, style: StyleBrief | None = None) -> Concept:
        """Decide what to make.

        A model plans it when one is configured; the router plans it when one
        is not, or when the model's reply cannot be used. Either way the
        result is checked - a host that does not exist, a dependency on an
        assignment nobody was given - before anybody starts work.
        """
        concept = self._from_model(brief) if self.ai is not None else None
        if concept is None:
            concept = self._from_rules(brief, style)
        else:
            concept.assignments = self._attach_recipes(brief, concept.assignments, style)
        concept.questions = [
            question for question in concept.questions if question.id not in brief.answers
        ]
        return concept

    # ------------------------------------------------------------- model
    def _from_model(self, brief: Brief) -> Concept | None:
        from app.ai.base import ChatMessage, TextRequest

        message = json.dumps(brief.to_dict(), ensure_ascii=False, indent=2)
        request = TextRequest(
            messages=[
                ChatMessage("system", DIRECTOR_PROMPT),
                ChatMessage("user", f"The brief:\n{message}\n\nPlan the work."),
            ],
            temperature=0.6,
            max_tokens=1600,
            json_mode=True,
            metadata={"task": "art_direction"},
        )
        try:
            response = self.ai._complete(request, purpose="art direction")  # noqa: SLF001
            payload = response.json(required=False)
        except AppError as exc:
            log.warning("The art director's model could not be reached (%s); routing by rules", exc.message)
            return None
        except Exception as exc:  # noqa: BLE001 - a model failure must not stop the run
            log.warning("Art direction failed (%s); routing by rules", exc)
            return None
        return self._parse(payload)

    def _parse(self, payload: Any) -> Concept | None:
        """Read a model reply, keeping only what is actually workable."""
        if not isinstance(payload, dict):
            return None
        raw = payload.get("assignments")
        if not isinstance(raw, list) or not raw:
            return None
        assignments: list[Assignment] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip().lower()
            if host not in BUILDERS:
                log.warning("The director asked for an unknown host '%s'; that assignment is dropped", host)
                continue
            goal = str(item.get("goal") or "").strip()
            if not goal:
                continue
            assignments.append(
                Assignment(
                    id=str(item.get("id") or f"{host}_{index + 1}"),
                    host=host,
                    goal=goal[:400],
                    detail=str(item.get("detail") or "")[:4000],
                    depends_on=[str(name) for name in (item.get("depends_on") or []) if name],
                )
            )
        if not assignments:
            return None
        known = {item.id for item in assignments}
        for item in assignments:
            unknown = [name for name in item.depends_on if name not in known]
            if unknown:
                log.warning("'%s' depends on %s, which nobody was given; ignored", item.id, unknown)
                item.depends_on = [name for name in item.depends_on if name in known]
        return Concept(
            name=str(payload.get("concept") or "")[:200],
            rationale=str(payload.get("rationale") or "")[:1000],
            assignments=assignments,
            questions=_parse_questions(payload.get("questions")),
            source="model",
        )

    # ------------------------------------------------------------- rules
    def _from_rules(self, brief: Brief, style: StyleBrief | None) -> Concept:
        """Route the brief without a model.

        This is not a degraded path that produces nothing: it decides the same
        things - which applications, in which order, with what handed between
        them - from what the request says and what was attached, and hands
        each specialist a recipe that builds a finished piece.
        """
        hosts = self._hosts_for(brief)
        item = self._format_for(brief, hosts)
        assignments = self._assignments_for(brief, hosts, item, style)
        name = brief.title or (brief.request[:80] if brief.request else "Studio job")
        rationale = (
            f"{', '.join(hosts)} carries this brief: "
            + "; ".join(f"{a.host} {a.goal.lower()}" for a in assignments)
            + "."
        )
        return Concept(
            name=name,
            rationale=rationale,
            assignments=assignments,
            questions=self._questions_for(brief, hosts),
            source="rules",
        )

    def _hosts_for(self, brief: Brief) -> list[str]:
        """Which applications this job needs."""
        if brief.hosts:
            named = [host for host in brief.hosts if host in BUILDERS]
            if named:
                return named
        text = f"{brief.request} {brief.format}".lower()
        hosts: list[str] = []
        moving = brief.footage or any(
            word in text
            for word in (
                "video", "reel", "story", "clip", "film", "premiere", "edit", "cut",
                "footage", "motion", "tiktok", "short",
                "ویدیو", "فیلم", "کلیپ", "تدوین", "تیزر", "موشن", "ریلز", "استوری",
                "ویدئو", "نماهنگ", "تایم‌لپس",
            )
        )
        # Two kinds of paged word. One names a *structure* - a page, a spread,
        # a column - and always means InDesign. The other names a
        # *publication*, and "a cover for a magazine" is a cover: the magazine
        # is what the cover is for, not what is being made.
        structural = any(
            word in text
            for word in (
                "page", "spread", "pages", "indesign", "column", "columns", "layout",
                "صفحه", "صفحات", "ستون", "صفحه‌آرایی",
            )
        )
        publication = any(
            word in text
            for word in (
                "newspaper", "magazine", "brochure", "catalogue", "catalog", "issue",
                "editorial", "journal",
                "روزنامه", "مجله", "نشریه", "بروشور", "کاتالوگ",
            )
        )
        paged = structural or (publication and not _wants_a_composite(brief))
        flat = any(
            word in text
            for word in (
                "poster", "cover", "banner", "photoshop", "composite", "artwork",
                "logo", "thumbnail", "post", "billboard", "flyer",
                "پوستر", "کاور", "بنر", "طرح", "بیلبورد", "تراکت",
            )
        )
        # A photograph is Photoshop's to work on, whatever else is being
        # made: the feature image on a page is a composite, not a crop. With
        # nothing attached there is nothing for a second agent to build, and
        # the page still draws its own boxes through the same factory.
        if flat or brief.references:
            hosts.append("photoshop")
        if paged:
            hosts.append("indesign")
        if moving:
            hosts.append("premiere")
        if not hosts:
            # Nothing in the brief names a medium. A single image is the
            # smallest thing that can be delivered, and it is what almost
            # every unqualified request turns out to mean.
            hosts.append("photoshop")
        if "premiere" in hosts and "photoshop" not in hosts:
            # Type over footage is a design, not a Premiere title.
            hosts.insert(0, "photoshop")
        return hosts

    def _format_for(self, brief: Brief, hosts: list[str]) -> str:
        """The size to work at."""
        if brief.format:
            return brief.format
        if "premiere" in hosts:
            return "1080p"
        if "indesign" in hosts:
            return "A3"
        return "A3"

    def _assignments_for(
        self, brief: Brief, hosts: list[str], item: str, style: StyleBrief | None
    ) -> list[Assignment]:
        """One assignment per application, wired so the work actually flows.

        What Photoshop makes depends on where it is going. On its own it makes
        the finished piece. Feeding a page it makes the feature image that
        page places. Feeding an edit it makes the title card that lies over
        the footage - on a transparent ground, so it lies over rather than
        covers.
        """
        parts = _split_content(brief)
        stem = _slug(brief.title or brief.request or "job")
        photo = brief.references[0] if brief.references else ""
        out: list[Assignment] = []
        artwork_id = ""
        artwork_name = ""

        if "photoshop" in hosts:
            artwork_id = "artwork"
            if "premiere" in hosts:
                artwork_name = f"{stem}_title"
                recipe = recipes.title_card(
                    design=artwork_name,
                    format=item,
                    headline=parts["headline"],
                    kicker=parts["kicker"],
                    detail=parts["detail"],
                    brief=style,
                    language=brief.language,
                )
                goal = f"Build the title card '{artwork_name}' for the edit, on a transparent ground"
            elif "indesign" in hosts:
                artwork_name = f"{stem}_image"
                sheet = self._sheet(item, rtl=brief.language in ("fa", "ar"))
                recipe = recipes.feature_image(
                    design=artwork_name,
                    width_mm=round(sheet.span(sheet.columns - 2), 1),
                    height_mm=round(sheet.live_height * 0.28, 1),
                    dpi=self.dpi,
                    photo=photo,
                    caption=parts["caption"],
                    brief=style,
                    language=brief.language,
                )
                goal = f"Build the feature image '{artwork_name}' the page will place"
            elif _wants_a_composite(brief):
                # Two pictures, or a brief that asks for a cover: the subject
                # comes off its background and the headline is reversed out of
                # the band it sits on, which is a different piece of work from
                # a poster with a photograph behind it.
                artwork_name = f"{stem}_cover"
                sheet = self._sheet(item, rtl=brief.language in ("fa", "ar"))
                recipe = recipes.composite_cover(
                    design=artwork_name,
                    format=item,
                    sheet=sheet,
                    headline=parts["headline"],
                    kicker=parts["kicker"],
                    detail=parts["detail"],
                    photo=photo,
                    subject=brief.references[1] if len(brief.references) > 1 else photo,
                    brief=style,
                    language=brief.language,
                )
                goal = f"Composite the cover '{artwork_name}' at {item}"
            else:
                artwork_name = f"{stem}_poster"
                sheet = self._sheet(item, rtl=brief.language in ("fa", "ar"))
                recipe = recipes.poster(
                    design=artwork_name,
                    format=item,
                    sheet=sheet,
                    headline=parts["headline"],
                    kicker=parts["kicker"],
                    detail=parts["detail"],
                    photo=photo,
                    brief=style,
                    language=brief.language,
                )
                goal = f"Build the artwork '{artwork_name}' at {item}"
            out.append(
                Assignment(
                    id=artwork_id,
                    host="photoshop",
                    goal=goal,
                    detail=parts["all"],
                    recipe=recipe,
                )
            )

        if "indesign" in hosts:
            name = f"{stem}_page"
            sheet = self._sheet(item, columns=6, rtl=brief.language in ("fa", "ar"))
            recipe = recipes.news_page(
                document=name,
                format=item,
                sheet=sheet,
                headline=parts["headline"],
                body=parts["body"] or parts["detail"],
                kicker=parts["kicker"],
                # The page places what Photoshop built, not the raw
                # attachment: that is what the hand-off is for.
                photo=artwork_name or photo,
                caption="" if artwork_name else parts["caption"],
                sidebar=parts["sidebar"],
                sidebar_heading=parts["sidebar_heading"],
                brief=style,
            )
            out.append(
                Assignment(
                    id="page",
                    host="indesign",
                    goal=f"Lay out the page '{name}' at {item}",
                    detail=parts["all"],
                    depends_on=[artwork_id] if artwork_id else [],
                    recipe=recipe,
                )
            )

        if "premiere" in hosts:
            name = f"{stem}_edit"
            recipe = recipes.social_cut(
                edit=name,
                format=item,
                footage=list(brief.footage),
                overlay=artwork_name,
            )
            out.append(
                Assignment(
                    id="edit",
                    host="premiere",
                    goal=f"Cut the sequence '{name}' at {item}",
                    detail=parts["all"],
                    depends_on=[artwork_id] if artwork_id else [],
                    recipe=recipe,
                )
            )
        return out

    def _sheet(self, item: str, *, columns: int = 6, rtl: bool = True) -> recipes.Sheet:
        """Measure the sheet a recipe will lay out."""
        if self.formats is None:
            from app.formats.registry import FormatRegistry

            self.formats = FormatRegistry()
        return recipes.Sheet.resolve(self.formats, item, columns=columns, rtl=rtl)

    def _attach_recipes(
        self, brief: Brief, assignments: list[Assignment], style: StyleBrief | None
    ) -> list[Assignment]:
        """Give a model's assignments something to fall back on.

        The model chose the work; if its own next step cannot be read at some
        point in the loop, the specialist still has a way to finish rather
        than stopping half-built.
        """
        routed = {
            item.host: item.recipe
            for item in self._assignments_for(
                brief, [a.host for a in assignments], self._format_for(brief, [a.host for a in assignments]), style
            )
        }
        for assignment in assignments:
            if assignment.recipe is None:
                assignment.recipe = routed.get(assignment.host)
        return assignments

    # --------------------------------------------------------- questions
    def _questions_for(self, brief: Brief, hosts: list[str]) -> list[Question]:
        """Ask only what the brief has not already answered."""
        out: list[Question] = []
        if not brief.format:
            out.append(
                Question(
                    id="format",
                    question="What size should this be?",
                    why="Everything else follows from the size: the type scale, the crop, the grid.",
                    options=_format_options(hosts),
                )
            )
        if not brief.references and "photoshop" in hosts:
            out.append(
                Question(
                    id="imagery",
                    question="Where should the picture come from?",
                    why="A design built round a photograph and one built out of type are different pieces of work.",
                    options=[
                        Option("I will attach a photograph", "You send the image and it is built round it.", True),
                        Option("Generate one", "The configured image service makes it to the brief."),
                        Option("Type only", "No photograph: colour, rules and type carry it."),
                    ],
                )
            )
        if len(hosts) > 1:
            out.append(
                Question(
                    id="emphasis",
                    question="Which piece matters most?",
                    why="The crew can spend its attention where it counts rather than spreading it evenly.",
                    options=[Option(_host_label(host), TRADE[host], index == 0) for index, host in enumerate(hosts)],
                )
            )
        return out[:3]


def _parse_questions(raw: Any) -> list[Question]:
    """Read the questions out of a model reply."""
    if not isinstance(raw, list):
        return []
    out: list[Question] = []
    for index, item in enumerate(raw[:3]):
        if not isinstance(item, dict):
            continue
        text = str(item.get("question") or "").strip()
        if not text:
            continue
        options = []
        for entry in item.get("options") or []:
            if isinstance(entry, dict) and entry.get("label"):
                options.append(
                    Option(
                        label=str(entry["label"])[:120],
                        detail=str(entry.get("detail") or "")[:300],
                        recommended=bool(entry.get("recommended")),
                    )
                )
            elif isinstance(entry, str) and entry.strip():
                options.append(Option(label=entry[:120]))
        out.append(
            Question(
                id=str(item.get("id") or f"q{index + 1}"),
                question=text[:300],
                why=str(item.get("why") or "")[:300],
                options=options,
                multi=bool(item.get("multi")),
            )
        )
    return out


def _host_label(host: str) -> str:
    """The application's own name."""
    return {"photoshop": "Photoshop", "indesign": "InDesign", "premiere": "Premiere Pro"}[host]


def _format_options(hosts: list[str]) -> list[Option]:
    """Sizes worth offering for this kind of job."""
    if "premiere" in hosts:
        return [
            Option("Vertical, 1080x1920", "Reels, Shorts, TikTok and stories.", True),
            Option("Widescreen, 1920x1080", "YouTube and anything played on a screen."),
            Option("Square, 1080x1080", "A feed post that reads the same either way."),
        ]
    if "indesign" in hosts:
        return [
            Option("A3, 297x420 mm", "A tabloid page or a small poster.", True),
            Option("Broadsheet, 381x476 mm", "A full newspaper page."),
            Option("A4, 210x297 mm", "A magazine page or a leaflet."),
        ]
    return [
        Option("A3, 297x420 mm", "Printed: a poster that holds a wall.", True),
        Option("Instagram post, 1080x1080", "A feed post."),
        Option("Instagram story, 1080x1920", "Full screen on a phone."),
    ]


#: Words that name the kind of piece a picture desk builds rather than a
#: poster with a photograph behind it.
_COMPOSITE_WORDS = (
    "cover", "composite", "cut out", "cut-out", "cutout", "montage", "collage",
    "portrait", "masthead", "front page",
    "کاور", "جلد", "ترکیب", "کلاژ", "پرتره", "مونتاژ",
)


def _wants_a_composite(brief: Brief) -> bool:
    """Whether this brief calls for a built cover rather than a poster."""
    if len(brief.references) > 1:
        # A background and a subject: there is something to cut out.
        return True
    text = f"{brief.request} {brief.title}".lower()
    return any(word in text for word in _COMPOSITE_WORDS)


def _slug(text: str, limit: int = 32) -> str:
    """A name a tool will accept."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:limit].lower() or "job"


def _split_content(brief: Brief) -> dict[str, str]:
    """Take the copy apart the way a sub-editor would.

    The first line is the headline, a line before it in brackets or ending in
    a colon is the kicker, a paragraph marked off is the sidebar, and the rest
    is the body. Nothing is invented: a brief that gives one line produces a
    headline and nothing else.
    """
    text = (brief.content or "").strip()
    request = brief.request.strip()
    if not text:
        return {
            "headline": request[:120] or (brief.title or "Untitled"),
            "kicker": "",
            "detail": request[120:400],
            "body": "",
            "caption": "",
            "sidebar": "",
            "sidebar_heading": "",
            "all": request,
        }
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    lines = [line.strip() for line in blocks[0].splitlines() if line.strip()]
    kicker = ""
    if len(lines) > 1 and len(lines[0]) <= 40:
        kicker, lines = lines[0].strip("[]()"), lines[1:]
    headline = lines[0] if lines else request[:120]
    detail = " ".join(lines[1:])[:400]
    rest = blocks[1:]
    sidebar = ""
    sidebar_heading = ""
    caption = ""
    for block in list(rest):
        first = block.splitlines()[0].strip()
        lowered = first.lower()
        if lowered.startswith(("sidebar", "box", "کادر", "باکس")) and len(block.splitlines()) > 1:
            sidebar_heading = first.split(":", 1)[-1].strip() or first
            sidebar = "\n".join(block.splitlines()[1:]).strip()
            rest.remove(block)
        elif lowered.startswith(("caption", "عکس", "زیرنویس")):
            caption = block.split(":", 1)[-1].strip()
            rest.remove(block)
    body = "\n\n".join(rest).strip()
    return {
        "headline": headline,
        "kicker": kicker,
        "detail": detail or (body.split("\n")[0][:300] if body else ""),
        "body": body,
        "caption": caption,
        "sidebar": sidebar,
        "sidebar_heading": sidebar_heading,
        "all": text[:4000],
    }


# ---------------------------------------------------------------- studio ----

SPECIALIST_PROMPT = """You are {trade} in a studio, working in Adobe {host}.

An art director has given you one piece of work. You do it with the tools
below and nothing else. Every reply is a single JSON object:

{{"thought": "one short sentence", "tool": "<tool name>", "arguments": {{...}}}}

When the work is done:

{{"thought": "why it is finished", "done": true, "summary": "what you made"}}

How you work:
- Read before you build. resolve_format turns a size in words into numbers;
  inspect tells you what is actually there.
- Build the ground first and the type last, the way the piece is looked at.
- Measure before you finish: check what you made and correct what is wrong
  rather than declaring it done.
- A tool that returns an error is refusing the change. Read the message,
  correct the arguments, and do something else if it refuses twice.
- Other specialists publish their work by name. list_artefacts shows what is
  there; place it rather than remaking it.

{material}

Available tools:
{tools}
"""


class Specialist:
    """One agent, one application, one set of tools."""

    def __init__(
        self,
        host: str,
        context: StudioContext,
        *,
        ai: Any = None,
        bus: EventBus | None = None,
    ) -> None:
        self.host = host
        self.context = context
        self.ai = ai
        self.bus = bus
        self.registry: ToolRegistry = BUILDERS[host](
            context, PermissionPolicy(CAPABILITIES[host]), bus, author=host
        )

    def work(self, assignment: Assignment, token: CancelToken | None = None) -> SpecialistResult:
        """Carry out one assignment, bounded per §57."""
        if self.ai is None and assignment.recipe is None:
            return SpecialistResult(
                assignment=assignment,
                skipped=(
                    "No model is configured and the director gave this assignment no "
                    "recipe, so there is nothing to drive the loop."
                ),
            )
        agent = AutonomousAgent(
            self.ai,
            self.registry,
            max_iterations=assignment.max_iterations,
            timeout_seconds=assignment.timeout_seconds,
            max_retries=3,
            fallback=assignment.recipe.planner() if assignment.recipe else None,
            system_prompt=self._prompt(assignment),
        )
        self._emit(EventType.PIPELINE_STAGE, stage=f"{self.host}:{assignment.id}", goal=assignment.goal)
        try:
            run = agent.run(
                assignment.goal,
                context=self._context_text(assignment),
                token=token,
                on_step=lambda step: self._emit(
                    EventType.PIPELINE_PROGRESS,
                    host=self.host,
                    assignment=assignment.id,
                    step=step.index,
                    tool=step.tool,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - one specialist failing is not the run failing
            log.exception("The %s specialist failed on '%s'", self.host, assignment.id)
            return SpecialistResult(assignment=assignment, error=str(exc))
        return SpecialistResult(assignment=assignment, run=run)

    def _prompt(self, assignment: Assignment) -> str:
        material = f"The material you were given:\n{assignment.detail}" if assignment.detail else ""
        return SPECIALIST_PROMPT.format(
            trade=TRADE[self.host],
            host=_host_label(self.host),
            material=material,
            tools=self.registry.describe(),
        )

    def _context_text(self, assignment: Assignment) -> str:
        published = self.context.board.artefacts()
        lines = [f"Language: {self.context.language}"]
        if assignment.depends_on:
            lines.append("This follows on from: " + ", ".join(assignment.depends_on))
        if published:
            lines.append(
                "Already published by the rest of the crew:\n"
                + "\n".join(
                    f"  {item.name} ({item.kind}) by {item.author}"
                    + (f" -> {item.path}" if item.path else "")
                    for item in published[:30]
                )
            )
        return "\n".join(lines)

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)


class Studio:
    """The crew: an art director and one specialist per application."""

    def __init__(
        self,
        context: StudioContext,
        *,
        ai: Any = None,
        director: ArtDirector | None = None,
        bus: EventBus | None = None,
        max_parallel: int = 3,
    ) -> None:
        self.context = context
        self.ai = ai
        self.bus = bus or context.bus
        self.director = director or ArtDirector(ai, formats=context.formats, dpi=context.dpi)
        self.max_parallel = max(1, min(3, max_parallel))
        self._specialists: dict[str, Specialist] = {}

    def specialist(self, host: str) -> Specialist:
        """The specialist for one application, made on first use."""
        found = self._specialists.get(host)
        if found is None:
            found = self._specialists[host] = Specialist(host, self.context, ai=self.ai, bus=self.bus)
        return found

    # ------------------------------------------------------------- run
    def run(
        self,
        brief: Brief,
        *,
        token: CancelToken | None = None,
        ask: bool = True,
        style: StyleBrief | None = None,
    ) -> StudioRun:
        """Plan the work and carry it out.

        When the director has a question whose answer would change what gets
        made, and *ask* is set, the run stops with the question rather than
        guessing. Answer it in :attr:`Brief.answers` and run again.
        """
        started = time.monotonic()
        self.context.language = brief.language or self.context.language
        if style is None and brief.references and self.context.analyst is not None:
            style = self._read_references(brief)

        concept = self.director.plan(brief, style=style)
        out = StudioRun(brief=brief, concept=concept, questions=concept.questions)
        self._emit(EventType.PIPELINE_STARTED, concept=concept.name, assignments=len(concept.assignments))

        blocking = [q for q in concept.questions if q.id in _BLOCKING_QUESTIONS]
        if ask and blocking:
            out.stop_reason = "waiting for an answer"
            out.duration = time.monotonic() - started
            self._emit(
                EventType.APPROVAL_REQUIRED,
                questions=[question.to_dict() for question in blocking],
                concept=concept.name,
            )
            log.info("The studio is waiting on %d question(s)", len(blocking))
            return out

        try:
            out.results = self._carry_out(concept.assignments, token)
        finally:
            board = self.context.board
            out.artefacts = [item.to_dict() for item in board.artefacts()]
            out.notes = board.notes()
            out.duration = time.monotonic() - started
        if not out.stop_reason:
            if token is not None and token.cancelled:
                out.stop_reason = "cancelled"
            elif out.finished:
                out.stop_reason = "finished"
            else:
                failed = [r.assignment.id for r in out.results if not r.ok]
                out.stop_reason = f"{len(failed)} assignment(s) unfinished: {', '.join(failed)}"
        self._emit(
            EventType.PIPELINE_FINISHED,
            concept=concept.name,
            files=len(out.files),
            reason=out.stop_reason,
        )
        log.info(
            "Studio run '%s': %s, %d file(s) in %.1fs",
            concept.name,
            out.stop_reason,
            len(out.files),
            out.duration,
        )
        return out

    # ----------------------------------------------------------- waves
    def _carry_out(
        self, assignments: list[Assignment], token: CancelToken | None
    ) -> list[SpecialistResult]:
        """Run the assignments, in parallel wherever the work allows."""
        results: list[SpecialistResult] = []
        done: set[str] = set()
        pending = list(assignments)

        while pending:
            if token is not None and token.cancelled:
                results.extend(
                    SpecialistResult(assignment=item, skipped="the run was cancelled") for item in pending
                )
                break
            wave = [item for item in pending if all(name in done for name in item.depends_on)]
            if not wave:
                # A dependency nobody can satisfy: say so rather than hanging.
                blocked = ", ".join(
                    f"{item.id} (needs {', '.join(n for n in item.depends_on if n not in done)})"
                    for item in pending
                )
                log.error("The studio cannot start: %s", blocked)
                results.extend(
                    SpecialistResult(
                        assignment=item,
                        skipped=f"waiting on {', '.join(n for n in item.depends_on if n not in done)}, "
                        "which never finished",
                    )
                    for item in pending
                )
                break

            # Two agents cannot drive the same application at once, so a wave
            # runs one assignment per host and the rest wait for the next.
            by_host: dict[str, Assignment] = {}
            for item in wave:
                by_host.setdefault(item.host, item)
            # Anything left over stays in `pending` and goes in the next wave.
            batch = list(by_host.values())
            log.info(
                "Studio wave: %s at the same time",
                ", ".join(f"{item.host}/{item.id}" for item in batch),
            )
            results.extend(self._run_wave(batch, token))
            for item in batch:
                pending.remove(item)
            done.update(
                result.assignment.id
                for result in results[-len(batch) :]
                if result.ok
            )
            failed = [result for result in results[-len(batch) :] if not result.ok]
            for result in failed:
                # Nothing that was waiting on failed work can go ahead; say
                # which, rather than letting the next wave build on nothing.
                blocked = [item for item in pending if result.assignment.id in item.depends_on]
                for item in blocked:
                    pending.remove(item)
                    results.append(
                        SpecialistResult(
                            assignment=item,
                            skipped=f"'{result.assignment.id}' did not finish",
                        )
                    )
        return results

    def _run_wave(
        self, batch: list[Assignment], token: CancelToken | None
    ) -> list[SpecialistResult]:
        """Run one wave, each assignment on its own thread."""
        if len(batch) == 1:
            return [self.specialist(batch[0].host).work(batch[0], token)]
        with ThreadPoolExecutor(
            max_workers=min(self.max_parallel, len(batch)), thread_name_prefix="studio"
        ) as pool:
            futures = [
                pool.submit(self.specialist(item.host).work, item, token) for item in batch
            ]
            return [future.result() for future in futures]

    # ------------------------------------------------------ references
    def _read_references(self, brief: Brief) -> StyleBrief | None:
        """Turn what the user attached into the style the work inherits."""
        analyst = self.context.analyst
        if analyst is None:
            return None
        try:
            found = analyst.analyze(
                [Path(item) for item in brief.references],
                instruction=brief.request,
                language=brief.language,
            )
        except Exception as exc:  # noqa: BLE001 - a bad attachment is not a failed run
            log.warning("The references could not be read: %s", exc)
            return None
        if found.failures:
            self.context.board.note(
                "art director",
                f"{len(found.failures)} attachment(s) could not be read: "
                + ", ".join(Path(r.path).name for r in found.failures),
            )
        if found.brief and found.brief.palette:
            self.context.board.note("art director", f"The references say: {found.brief.describe()}")
            return found.brief
        return None

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)


#: Questions the run genuinely cannot proceed past. Everything else is asked
#: for the operator's benefit while the work goes ahead on a sound default.
_BLOCKING_QUESTIONS = {"format"}
