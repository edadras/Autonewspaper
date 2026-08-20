"""The agent tool surface, its permission policy and the bounded loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import (
    AgentContext,
    AutonomousAgent,
    Parameter,
    PermissionPolicy,
    Tool,
    ToolRegistry,
    build_toolset,
    qa_fallback_planner,
    verify_exists,
    verify_geometry,
)
from app.layout.engine import LayoutEngine
from app.models.schemas import LayoutPlan, Rect
from app.vision.qa_agent import VisionQAAgent
from app.vision.renderer import PreviewRenderer


@pytest.fixture
def agent_context(application, project):
    application.pipeline.run(project, mode="auto")
    plan = LayoutPlan.load(project.layout_plan_path)
    template = application.templates.get(plan.template_id)
    engine = LayoutEngine(template, language=plan.language)
    return AgentContext(
        handle=project,
        template=template,
        plan=plan,
        engine=engine,
        adobe=application.adobe,
        assets=application.assets,
        exporter=application.exporter,
        qa=VisionQAAgent(engine, template, ai=application.ai, threshold=90, use_vision_model=False),
        renderer=PreviewRenderer(template, dpi=90),
        undo=application.undo,
        bus=application.bus,
    )


# ----------------------------------------------------------------- protocol


def test_arguments_are_validated_before_anything_runs():
    registry = ToolRegistry(PermissionPolicy({"layout.write"}))
    registry.register(
        Tool(
            "move", "move a frame",
            [
                Parameter("element_id", "string", "id"),
                Parameter("x_mm", "number", "x", minimum=0, maximum=500),
            ],
            handler=lambda element_id, x_mm: {"id": element_id, "x": x_mm, "y": 0, "width": 1, "height": 1},
            capability="layout.write",
            verifier=verify_geometry,
        )
    )
    assert registry.invoke("move", {"element_id": "a", "x_mm": 10}).ok
    assert "below the minimum" in registry.invoke("move", {"element_id": "a", "x_mm": -1}).error
    assert "unknown argument" in registry.invoke("move", {"element_id": "a", "x_mm": 1, "z": 2}).error
    assert "missing argument" in registry.invoke("move", {"element_id": "a"}).error
    assert "Unknown tool" in registry.invoke("nope", {}).error


def test_a_denied_capability_blocks_the_call():
    registry = ToolRegistry(PermissionPolicy.read_only())
    registry.register(
        Tool("write", "writes", [], handler=lambda: True, capability="layout.write")
    )
    result = registry.invoke("write", {})
    assert not result.ok
    assert "not granted" in result.error


def test_results_are_verified_against_the_real_state(tmp_path):
    registry = ToolRegistry(PermissionPolicy({"export.write"}))
    registry.register(
        Tool(
            "export", "claims to export", [Parameter("path", "string", "out")],
            handler=lambda path: {"path": path},
            capability="export.write",
            verifier=verify_exists,
        )
    )
    missing = registry.invoke("export", {"path": str(tmp_path / "nope.pdf")})
    assert not missing.ok
    assert missing.verified is False

    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4\n")
    assert registry.invoke("export", {"path": str(real)}).ok


def test_a_failing_tool_never_escapes():
    registry = ToolRegistry()
    registry.register(Tool("bad", "raises", [], handler=lambda: 1 / 0, capability="layout.read"))
    result = registry.invoke("bad", {})
    assert not result.ok
    assert "ZeroDivisionError" in result.error


# ------------------------------------------------------------------ toolset


def test_every_specified_tool_is_registered(agent_context):
    registry = build_toolset(agent_context)
    for name in (
        "open_indesign", "open_photoshop", "create_page", "create_text_frame",
        "create_image_frame", "place_image", "insert_text", "resize_element",
        "move_element", "apply_style", "render_page", "analyze_page",
        "generate_image", "process_image", "export_pdf", "save_project",
    ):
        assert name in registry.names(), f"{name} is missing from the tool surface"


def test_the_tools_refuse_to_break_the_page(agent_context):
    registry = build_toolset(agent_context)
    page = agent_context.plan.pages[0]
    element = [e for e in page.elements if not e.locked][0]

    off_page = registry.invoke(
        "move_element", {"element_id": element.id, "x_mm": 1900, "y_mm": 10}
    )
    assert not off_page.ok

    other = [e for e in page.elements if not e.locked and e.id != element.id][0]
    overlap = registry.invoke(
        "move_element",
        {"element_id": element.id, "x_mm": other.rect.x, "y_mm": other.rect.y},
    )
    assert not overlap.ok
    assert "overlap" in overlap.error.lower()


def test_locked_master_furniture_cannot_be_moved(agent_context):
    registry = build_toolset(agent_context)
    page = agent_context.plan.pages[0]
    locked = [e for e in page.elements if e.locked]
    if not locked:
        pytest.skip("this template places no master furniture on the page")
    result = registry.invoke(
        "move_element", {"element_id": locked[0].id, "x_mm": 20, "y_mm": 20}
    )
    assert not result.ok
    assert "locked" in result.error.lower()


def test_render_and_analyze_produce_real_output(agent_context):
    registry = build_toolset(agent_context)
    rendered = registry.invoke("render_page", {"page": 1})
    assert rendered.ok
    assert Path(rendered.data["path"]).exists()

    analysis = registry.invoke("analyze_page", {"page": 1})
    assert analysis.ok
    assert 0 <= analysis.data["score"] <= 100
    assert "elements" in analysis.data and analysis.data["elements"]


def test_export_through_the_tool_writes_a_pdf(agent_context):
    registry = build_toolset(agent_context)
    result = registry.invoke("export_pdf", {"preset": "web"})
    assert result.ok
    assert Path(result.data["path"]).exists()


# -------------------------------------------------------------------- loop


def test_the_agent_fixes_a_frame_it_can_reach(application, agent_context):
    page = agent_context.plan.pages[0]
    body = [e for e in page.elements if e.type.value == "body" and not e.locked][0]
    body.rect = Rect(x=1.5, y=body.rect.y, width=body.rect.width, height=body.rect.height)

    registry = build_toolset(agent_context)
    agent = AutonomousAgent(
        application.ai, registry, max_iterations=8, timeout_seconds=90, max_retries=2,
        fallback=qa_fallback_planner(1),
    )
    run = agent.run("Bring page 1 back within its margins")

    assert run.tool_calls >= 2
    assert any(step.tool == "move_element" and step.result and step.result.ok for step in run.steps)
    assert body.rect.x >= page.content_rect.x - 0.5
    assert run.finished or run.stop_reason


def test_the_loop_is_bounded_by_its_iteration_limit(application, agent_context):
    registry = build_toolset(agent_context)
    agent = AutonomousAgent(
        application.ai, registry, max_iterations=3, timeout_seconds=60, max_retries=5,
        fallback=lambda steps: __import__(
            "app.agents.autonomous", fromlist=["AgentStep"]
        ).AgentStep(index=0, thought="loop", tool="render_page", arguments={"page": 1}),
    )
    run = agent.run("never satisfied")
    assert len(run.steps) <= 3
    assert run.stop_reason


def test_the_loop_stops_after_repeated_failures(application, agent_context):
    from app.agents.autonomous import AgentStep

    registry = build_toolset(agent_context)
    agent = AutonomousAgent(
        application.ai, registry, max_iterations=10, timeout_seconds=60, max_retries=1,
        fallback=lambda steps: AgentStep(
            index=0, thought="bad call", tool="move_element",
            arguments={"element_id": f"missing_{len(steps)}", "x_mm": 10, "y_mm": 10},
        ),
    )
    run = agent.run("impossible goal")
    assert not run.finished
    assert "failure" in run.stop_reason
    assert run.failures >= 2


def test_the_editorial_agent_leaves_the_body_alone(application, project):
    from app.agents.editorial_agent import EditorialAgent

    agent = EditorialAgent(application.ai)
    with project.uow() as uow:
        article = uow.articles.for_project(project.project_id)[0]
        original_body, article_id = article.body, article.id

    suggestion = agent.suggest_headline(project, article_id)
    assert suggestion.headline
    agent.apply_headline(project, article_id, suggestion)
    agent.summarize(project, article_id)

    with project.uow() as uow:
        updated = uow.articles.get(article_id)
    assert updated.body == original_body
    assert updated.original_title
    assert agent.restore_original_title(project, article_id)
