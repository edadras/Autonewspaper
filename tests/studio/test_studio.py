"""The crew: an art director and three specialists working at once."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.agents.studio import (
    ArtDirector,
    Assignment,
    Brief,
    Concept,
    Specialist,
    Studio,
    _split_content,
)
from app.core.jobs import CancelToken

# --------------------------------------------------------------- routing ---


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("یک پوستر برای کنسرت", ["photoshop"]),
        ("a poster for the concert", ["photoshop"]),
        ("صفحه اول روزنامه فردا", ["indesign"]),
        ("lay out a magazine spread", ["indesign"]),
        ("یک ریلز اینستاگرام بساز", ["photoshop", "premiere"]),
        ("cut a video for youtube", ["photoshop", "premiere"]),
        ("طرح جلد مجله و صفحات داخلی", ["photoshop", "indesign"]),
    ],
)
def test_the_director_routes_the_brief_to_the_right_applications(
    request_text: str, expected: list[str]
) -> None:
    director = ArtDirector()

    hosts = director._hosts_for(Brief(request=request_text))

    assert hosts == expected


def test_type_over_footage_is_designed_in_photoshop_first() -> None:
    """A title on a video is a design, not a Premiere title."""
    director = ArtDirector()

    hosts = director._hosts_for(Brief(request="تدوین کلیپ"))

    assert hosts.index("photoshop") < hosts.index("premiere")


def test_an_unqualified_request_still_gets_made() -> None:
    director = ArtDirector()

    hosts = director._hosts_for(Brief(request="یک چیز قشنگ بساز"))

    assert hosts == ["photoshop"]


def test_the_user_naming_applications_overrules_the_router() -> None:
    director = ArtDirector()

    hosts = director._hosts_for(Brief(request="a poster", hosts=["premiere", "indesign"]))

    assert hosts == ["premiere", "indesign"]


def test_an_application_that_does_not_exist_is_ignored() -> None:
    director = ArtDirector()

    hosts = director._hosts_for(Brief(request="a poster", hosts=["illustrator"]))

    assert hosts == ["photoshop"]


# -------------------------------------------------------------- the copy ---


def test_the_copy_is_taken_apart_the_way_a_sub_editor_would() -> None:
    brief = Brief(
        request="صفحه یک",
        content=(
            "(گزارش ویژه)\n"
            "دولت لایحه بودجه را فرستاد\n"
            "سقف درآمد نفتی بازنگری شد\n\n"
            "بند اول متن اصلی.\n\nبند دوم متن اصلی.\n\n"
            "کادر: نکته‌های کلیدی\nسه نکته کوتاه.\n\n"
            "عکس: صحن علنی مجلس"
        ),
    )

    parts = _split_content(brief)

    assert parts["kicker"] == "گزارش ویژه"
    assert parts["headline"] == "دولت لایحه بودجه را فرستاد"
    assert parts["sidebar_heading"] == "نکته‌های کلیدی"
    assert "سه نکته کوتاه" in parts["sidebar"]
    assert parts["caption"] == "صحن علنی مجلس"
    assert "بند اول" in parts["body"] and "بند دوم" in parts["body"]
    assert "نکته‌های کلیدی" not in parts["body"], "the sidebar is not set twice"


def test_nothing_is_invented_from_a_one_line_brief() -> None:
    parts = _split_content(Brief(request="پوستر جشنواره"))

    assert parts["headline"] == "پوستر جشنواره"
    assert parts["body"] == ""
    assert parts["sidebar"] == ""


# ---------------------------------------------------------- the hand-off ---


def test_photoshop_makes_what_its_destination_needs(studio_context, photograph) -> None:
    """A poster on its own, a feature image for a page, a title card for an edit."""
    director = ArtDirector(formats=studio_context.formats)
    picture = [str(photograph)]

    alone = director.plan(Brief(request="یک پوستر", format="A3", title="t"))
    for_page = director.plan(
        Brief(request="یک صفحه روزنامه", format="A3", title="t", references=picture)
    )
    for_edit = director.plan(
        Brief(request="یک ریلز", format="instagram reel", title="t", references=picture)
    )

    assert alone.assignments[0].recipe.name.startswith("poster")
    assert for_page.assignments[0].recipe.name.startswith("feature image")
    assert for_edit.assignments[0].recipe.name.startswith("title card")


def test_a_page_with_no_imagery_draws_its_own_boxes(studio_context) -> None:
    """There is nothing for a second agent to composite, and it still gets its furniture."""
    director = ArtDirector(formats=studio_context.formats)

    concept = director.plan(
        Brief(request="یک صفحه روزنامه", format="A3", title="t", content="سرخط\n\nکادر: نکته\nمتن")
    )

    assert [item.host for item in concept.assignments] == ["indesign"]
    page = concept.assignments[0]
    assert any(name == "add_page_furniture" for name, _ in page.recipe.calls)


def test_the_page_places_what_photoshop_built(studio_context, photograph) -> None:
    director = ArtDirector(formats=studio_context.formats)

    concept = director.plan(
        Brief(request="صفحه روزنامه", format="A3", title="t", references=[str(photograph)])
    )

    page = next(item for item in concept.assignments if item.host == "indesign")
    artwork = next(item for item in concept.assignments if item.host == "photoshop")
    assert artwork.id in page.depends_on
    placed = [
        call for name, call in page.recipe.calls if name == "add_picture_frame"
    ]
    assert placed and placed[0]["path"] == "t_image", "the page places the artwork by name"


def test_the_edit_lays_the_title_card_over_the_footage(studio_context, tmp_path) -> None:
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    director = ArtDirector(formats=studio_context.formats)

    concept = director.plan(
        Brief(
            request="ریلز",
            format="instagram reel",
            title="t",
            footage=[str(clip)],
            hosts=["photoshop", "premiere"],
        )
    )

    edit = next(item for item in concept.assignments if item.host == "premiere")
    overlays = [call for name, call in edit.recipe.calls if name == "add_overlay"]
    assert overlays and overlays[0]["path"] == "t_title"
    assert overlays[0]["track"] >= 1


# ------------------------------------------------------------- questions ---


def test_a_missing_size_stops_the_run_and_asks(studio_context) -> None:
    """Everything follows from the size, so it is worth waiting for."""
    run = Studio(studio_context).run(Brief(request="یک پوستر بساز"))

    assert run.stop_reason == "waiting for an answer"
    assert not run.results
    question = next(item for item in run.questions if item.id == "format")
    assert question.options
    assert any(option.recommended for option in question.options)


def test_an_answered_question_is_not_asked_again(studio_context) -> None:
    brief = Brief(request="یک پوستر بساز", answers={"format": "A3"}, format="A3")

    run = Studio(studio_context).run(brief)

    assert run.stop_reason != "waiting for an answer"
    assert not [item for item in run.questions if item.id == "format"]


def test_questions_that_do_not_block_are_asked_alongside_the_work(studio_context) -> None:
    brief = Brief(request="یک پوستر و یک صفحه", format="A3", title="t")

    run = Studio(studio_context).run(brief)

    assert run.results, "the work went ahead"
    assert any(item.id in ("imagery", "emphasis") for item in run.questions)


def test_the_director_asks_at_most_three_things(studio_context) -> None:
    director = ArtDirector(formats=studio_context.formats)

    concept = director.plan(Brief(request="یک ریلز و یک صفحه و یک پوستر"))

    assert len(concept.questions) <= 3


# ----------------------------------------------------------------- waves ---


def test_independent_specialists_run_at_the_same_time(studio_context, tmp_path) -> None:
    """Three applications are three processes; there is nothing to wait for."""
    overlap = threading.Event()
    inside = threading.Semaphore(0)
    started: list[str] = []
    lock = threading.Lock()

    def slow_work(self, assignment, token=None):  # noqa: ANN001, ARG001
        with lock:
            started.append(assignment.host)
            count = len(started)
        if count >= 2:
            overlap.set()
        # Hold long enough that a serial runner could not overlap us.
        overlap.wait(timeout=5.0)
        from app.agents.autonomous import AgentRun
        from app.agents.studio import SpecialistResult

        return SpecialistResult(
            assignment=assignment, run=AgentRun(goal=assignment.goal, finished=True)
        )

    concept = Concept(
        assignments=[
            Assignment(id="a", host="photoshop", goal="one"),
            Assignment(id="b", host="indesign", goal="two"),
            Assignment(id="c", host="premiere", goal="three"),
        ]
    )
    studio = Studio(studio_context, director=_FixedDirector(concept))

    original = Specialist.work
    Specialist.work = slow_work  # type: ignore[method-assign]
    try:
        started_at = time.monotonic()
        run = studio.run(Brief(request="x", format="A3"))
    finally:
        Specialist.work = original  # type: ignore[method-assign]
        del inside

    assert overlap.is_set(), "two specialists were never inside at the same time"
    assert len(run.results) == 3
    assert time.monotonic() - started_at < 5.0


def test_a_dependent_assignment_waits_for_what_it_needs(studio_context) -> None:
    order: list[str] = []

    def record(self, assignment, token=None):  # noqa: ANN001, ARG001
        order.append(assignment.id)
        from app.agents.autonomous import AgentRun
        from app.agents.studio import SpecialistResult

        return SpecialistResult(
            assignment=assignment, run=AgentRun(goal=assignment.goal, finished=True)
        )

    concept = Concept(
        assignments=[
            Assignment(id="page", host="indesign", goal="two", depends_on=["artwork"]),
            Assignment(id="artwork", host="photoshop", goal="one"),
        ]
    )
    studio = Studio(studio_context, director=_FixedDirector(concept))

    original = Specialist.work
    Specialist.work = record  # type: ignore[method-assign]
    try:
        studio.run(Brief(request="x", format="A3"))
    finally:
        Specialist.work = original  # type: ignore[method-assign]

    assert order == ["artwork", "page"]


def test_two_agents_never_drive_the_same_application_at_once(studio_context) -> None:
    """Photoshop has one active document; two agents in it would fight."""
    live = {"photoshop": 0}
    clashes: list[int] = []
    lock = threading.Lock()

    def work(self, assignment, token=None):  # noqa: ANN001, ARG001
        with lock:
            live[self.host] = live.get(self.host, 0) + 1
            if live[self.host] > 1:
                clashes.append(live[self.host])
        time.sleep(0.05)
        with lock:
            live[self.host] -= 1
        from app.agents.autonomous import AgentRun
        from app.agents.studio import SpecialistResult

        return SpecialistResult(
            assignment=assignment, run=AgentRun(goal=assignment.goal, finished=True)
        )

    concept = Concept(
        assignments=[
            Assignment(id="a", host="photoshop", goal="one"),
            Assignment(id="b", host="photoshop", goal="two"),
            Assignment(id="c", host="photoshop", goal="three"),
        ]
    )
    studio = Studio(studio_context, director=_FixedDirector(concept))

    original = Specialist.work
    Specialist.work = work  # type: ignore[method-assign]
    try:
        run = studio.run(Brief(request="x", format="A3"))
    finally:
        Specialist.work = original  # type: ignore[method-assign]

    assert not clashes
    assert len(run.results) == 3, "all three were still done, one after another"


def test_work_that_depends_on_a_failure_is_not_started(studio_context) -> None:
    def fail_first(self, assignment, token=None):  # noqa: ANN001, ARG001
        from app.agents.studio import SpecialistResult

        if assignment.id == "artwork":
            return SpecialistResult(assignment=assignment, error="Photoshop refused")
        raise AssertionError("the page should never have started")

    concept = Concept(
        assignments=[
            Assignment(id="artwork", host="photoshop", goal="one"),
            Assignment(id="page", host="indesign", goal="two", depends_on=["artwork"]),
        ]
    )
    studio = Studio(studio_context, director=_FixedDirector(concept))

    original = Specialist.work
    Specialist.work = fail_first  # type: ignore[method-assign]
    try:
        run = studio.run(Brief(request="x", format="A3"))
    finally:
        Specialist.work = original  # type: ignore[method-assign]

    assert not run.finished
    page = next(item for item in run.results if item.assignment.id == "page")
    assert "artwork" in page.skipped


def test_a_dependency_on_nothing_is_reported_rather_than_hung(studio_context) -> None:
    concept = Concept(
        assignments=[Assignment(id="page", host="indesign", goal="two", depends_on=["ghost"])]
    )
    studio = Studio(studio_context, director=_FixedDirector(concept))

    run = studio.run(Brief(request="x", format="A3"))

    assert not run.finished
    assert "ghost" in run.results[0].skipped


def test_cancelling_stops_the_crew(studio_context) -> None:
    token = CancelToken()
    token.cancel()
    concept = Concept(assignments=[Assignment(id="a", host="photoshop", goal="one")])
    studio = Studio(studio_context, director=_FixedDirector(concept))

    run = studio.run(Brief(request="x", format="A3"), token=token)

    assert run.stop_reason == "cancelled"


# ------------------------------------------------------------ end to end ---


def test_a_whole_job_is_produced_with_no_model_and_no_adobe(studio_context, photograph) -> None:
    """The system has to work on the machine it is actually installed on."""
    brief = Brief(
        request="یک صفحه روزنامه برای گزارش بودجه",
        title="budget",
        content=(
            "(گزارش ویژه)\n"
            "دولت لایحه بودجه را به مجلس فرستاد\n\n"
            + "متن اصلی گزارش. " * 60
            + "\n\nکادر: نکته‌ها\n"
            + "سه نکته کوتاه. " * 8
        ),
        references=[str(photograph)],
        format="381x476 mm",
        language="fa",
    )

    run = Studio(studio_context).run(brief)

    assert run.finished, run.stop_reason
    assert [item.host for item in run.concept.assignments] == ["photoshop", "indesign"]
    assert all(result.run and result.run.failures == 0 for result in run.results)

    produced = {Path(path).name for path in run.files}
    assert any(name.endswith(".pdf") for name in produced)
    assert "budget_image.png" in produced, "Photoshop built the feature image"

    layout = json.loads(
        next(Path(path) for path in run.files if path.endswith("_layout.json")).read_text()
    )
    placed = [
        element["image_path"]
        for page in layout["pages"]
        for element in page["elements"]
        if element.get("image_path")
    ]
    assert any("budget_image" in path for path in placed), "InDesign placed it"
    assert any("tint_panel" in path for path in placed), "the sidebar box was drawn in Photoshop"


def test_every_produced_file_actually_exists(studio_context) -> None:
    run = Studio(studio_context).run(
        Brief(request="یک پوستر", title="p", format="A4", content="سرخط پوستر")
    )

    assert run.files
    for path in run.files:
        assert Path(path).exists(), path
        assert Path(path).stat().st_size > 0


def test_the_run_can_be_serialised_for_the_interface(studio_context) -> None:
    run = Studio(studio_context).run(
        Brief(request="یک پوستر", title="p", format="A4", content="سرخط")
    )

    payload = json.dumps(run.to_dict(), ensure_ascii=False, default=str)

    assert "assignments" in payload
    assert len(payload) > 500


class _FixedDirector:
    """A director that has already made up its mind."""

    def __init__(self, concept: Concept) -> None:
        self.concept = concept

    def plan(self, brief: Brief, *, style=None) -> Concept:  # noqa: ANN001, ARG002
        return self.concept
