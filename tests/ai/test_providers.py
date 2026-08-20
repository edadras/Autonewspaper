"""The provider abstraction, the offline analyser and prompt management."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.ai.base import TextRequest, extract_json
from app.ai.heuristic_provider import HeuristicProvider
from app.ai.images import DisabledImageProvider
from app.ai.prompts import PromptLibrary
from app.ai.registry import AIService, build_text_provider
from app.models.schemas import ImageRequest


def run(coroutine):
    """Run a coroutine in a fresh event loop."""
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    "payload",
    [
        '{"a": 1}',
        'prose before {"a": 1} prose after',
        '```json\n{"a": 1}\n```',
        '{"a": 1,}',
    ],
)
def test_json_is_extracted_from_messy_replies(payload):
    assert extract_json(payload) == {"a": 1}


def test_unparsable_reply_returns_none():
    assert extract_json("no json here") is None


def test_offline_provider_scores_and_categorises():
    provider = HeuristicProvider()
    request = TextRequest.simple("s", "u")
    request.metadata = {
        "task": "editorial_analysis",
        "data": {
            "articles": [
                {"id": 1, "title": "زلزله شدید در غرب کشور", "body": "زلزله و امدادرسانی. " * 30},
                {"id": 2, "title": "پیروزی تیم ملی فوتبال", "body": "بازی و گل و مسابقه. " * 25},
                {"id": 3, "title": "Global climate summit", "body": "World leaders met in Europe. " * 25},
            ],
            "page_count": 3,
        },
    }
    payload = run(provider.generate_text(request)).json()
    categories = {a["article_id"]: a["category"] for a in payload["analyses"]}
    assert categories[1] == "incident"
    assert categories[2] == "sport"
    assert categories[3] == "world"
    assert payload["page_assignments"]["1"], "the front page must never be left empty"
    for analysis in payload["analyses"]:
        assert 0 <= analysis["importance"] <= 100
        assert 1 <= analysis["recommended_page"] <= 3


def test_offline_provider_is_deterministic():
    provider = HeuristicProvider()
    request = TextRequest.simple("s", "u")
    request.metadata = {
        "task": "editorial_analysis",
        "data": {"articles": [{"id": 1, "title": "خبر", "body": "متن " * 40}], "page_count": 2},
    }
    first = run(provider.generate_text(request)).text
    second = run(provider.generate_text(request)).text
    assert first == second


def test_offline_provider_reports_healthy_without_a_network():
    health = run(HeuristicProvider().health_check())
    assert health.available
    assert not HeuristicProvider().requires_network


def test_offline_vision_measures_a_real_image(tmp_path):
    from tests.conftest import make_image
    from app.ai.base import VisionRequest

    image = make_image(tmp_path / "p.jpg")
    response = run(HeuristicProvider().analyze_image(VisionRequest([image], "review")))
    payload = json.loads(response.text)
    assert payload["images"][0]["width"] == 2000
    assert payload["score"] > 0


def test_disabled_image_provider_marks_the_placeholder(tmp_path):
    provider = DisabledImageProvider()
    generated = run(provider.generate(ImageRequest(subject="زلزله", width_px=800, height_px=450),
                                     tmp_path / "x.png"))
    assert Path(generated.path).exists()
    sidecar = json.loads(Path(str(generated.path) + ".ai.json").read_text(encoding="utf-8"))
    assert sidecar["placeholder"] is True
    assert sidecar["ai_generated"] is False


def test_generated_image_metadata_records_provenance(tmp_path):
    from app.models.schemas import GeneratedImage

    metadata = GeneratedImage(
        path=str(tmp_path / "a.png"), provider="openai", model="gpt-image-1",
        prompt="p", width=10, height=10,
    ).metadata()
    assert metadata["ai_generated"] is True
    assert metadata["provider"] == "openai"
    assert metadata["timestamp"]


def test_prompt_library_loads_and_renders():
    root = Path(__file__).resolve().parents[2] / "prompts"
    library = PromptLibrary(root)
    assert library.load() >= 7
    assert "editorial/analysis" in library.keys()
    prompt = library.get("editorial/analysis")
    assert prompt.task == "editorial_analysis"
    system, user = prompt.render(
        publication_name="P", edition_date="d", language="fa", page_count=4,
        design_style="classic", max_headline_chars=70, articles_json="[]",
    )
    assert "{" in user and "{{" not in user
    assert "page_count" not in user.split("Pages available:")[0]


def test_prompt_render_refuses_missing_values():
    from app.core.errors import ConfigurationError

    root = Path(__file__).resolve().parents[2] / "prompts"
    with pytest.raises(ConfigurationError):
        PromptLibrary(root).get("editorial/analysis").render(language="fa")


def test_unknown_provider_falls_back_to_the_offline_analyser(settings):
    provider = build_text_provider("nonexistent", "x", settings)
    assert isinstance(provider, HeuristicProvider)


def test_service_degrades_when_the_provider_raises(settings, monkeypatch):
    service = AIService(settings)
    try:
        class Broken(HeuristicProvider):
            name = "broken"

            async def generate_text(self, request):  # noqa: D102
                raise RuntimeError("vendor outage")

        service.text_provider = Broken()
        plan = service.analyze_articles(
            [{"id": 1, "title": "خبر مهم", "body": "متن خبر. " * 30}],
            project_id=1, publication_name="P", edition_date="2026-01-01",
            language="fa", page_count=2, design_style="classic",
        )
        assert plan.analyses, "the run must continue with the offline analyser"
        assert "broken" in service.describe()["degraded"]
    finally:
        service.close()


def test_service_health_covers_text_vision_and_images(settings):
    service = AIService(settings)
    try:
        names = [h.name for h in service.health()]
        assert any(n.startswith("text:") for n in names)
        assert any(n.startswith("vision:") for n in names)
        assert any(n.startswith("image:") for n in names)
    finally:
        service.close()
