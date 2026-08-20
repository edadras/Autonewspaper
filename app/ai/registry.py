"""Provider registry and the AI service facade.

:class:`AIService` is the single entry point the rest of the application uses.
It owns the configured providers, renders prompts, runs the coroutine on a
private event loop (so worker threads can call it synchronously) and falls
back to the offline analyser when a cloud provider fails - a vendor outage
degrades quality, it never aborts a run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.base import AIProvider, AIResponse, ProviderHealth, TextRequest, VisionRequest
from app.ai.gemini_provider import GeminiProvider
from app.ai.heuristic_provider import HeuristicProvider
from app.ai.images import IMAGE_PROVIDERS, DisabledImageProvider, ImageGenerationProvider
from app.ai.local_provider import LocalProvider
from app.ai.openai_provider import OpenAIProvider
from app.ai.prompts import PromptLibrary
from app.config.settings import SettingsManager
from app.core.errors import AIProviderError, AppError
from app.models.schemas import (
    ArticleAnalysis,
    AreaKind,
    EditorialPlan,
    GeneratedImage,
    ImageRequest,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

TEXT_PROVIDERS: dict[str, type[AIProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "local": LocalProvider,
    "heuristic": HeuristicProvider,
}


def build_text_provider(
    name: str, model: str, settings: SettingsManager, *, base_url: str | None = None, **extra: Any
) -> AIProvider:
    """Instantiate the text/vision provider called *name*."""
    factory = TEXT_PROVIDERS.get(name)
    if factory is None:
        log.error("Unknown AI provider '%s'; falling back to the offline analyser", name)
        return HeuristicProvider()
    if factory is HeuristicProvider:
        # The offline analyser has no external model; keep its own identifier so
        # logs and reports never claim a vendor model was used.
        return HeuristicProvider("rule-based-v1")
    ai = settings.settings.ai
    return factory(
        model,
        api_key=settings.api_key(name) or "",
        base_url=base_url or ai.base_url,
        timeout=ai.timeout_seconds,
        max_retries=ai.max_retries,
        **extra,
    )


def build_image_provider(settings: SettingsManager) -> ImageGenerationProvider:
    """Instantiate the configured image generation provider."""
    config = settings.settings.image_ai
    factory = IMAGE_PROVIDERS.get(config.provider, DisabledImageProvider)
    if factory is DisabledImageProvider:
        return DisabledImageProvider()
    return factory(
        config.model,
        api_key=settings.api_key(config.provider) or "",
        base_url=config.base_url,
        timeout=config.timeout_seconds,
    )


class _LoopThread:
    """A private asyncio loop so synchronous callers can await coroutines."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="ains-ai-loop", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run *coro* on the loop and block until it finishes."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def stop(self) -> None:
        """Stop the loop and join the thread."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        try:
            self.loop.close()
        except Exception:  # pragma: no cover
            pass


class AIService:
    """High-level, synchronous AI operations used by the agents and pipeline."""

    def __init__(
        self,
        settings: SettingsManager,
        prompts: PromptLibrary | None = None,
        *,
        loop: _LoopThread | None = None,
    ) -> None:
        self.settings = settings
        self.prompts = prompts or PromptLibrary(settings.paths.prompts)
        self.prompts.load()
        self._loop = loop or _LoopThread()
        self._owns_loop = loop is None
        self._offline = HeuristicProvider()
        self.text_provider = build_text_provider(
            settings.settings.ai.provider, settings.settings.ai.model, settings
        )
        self.vision_provider = build_text_provider(
            settings.settings.ai.vision_provider, settings.settings.ai.vision_model, settings
        )
        self.image_provider = build_image_provider(settings)
        self._degraded: set[str] = set()

    # ------------------------------------------------------------ plumbing
    def reload(self) -> None:
        """Rebuild the providers after the settings changed."""
        self.close(keep_loop=True)
        config = self.settings.settings.ai
        self.text_provider = build_text_provider(config.provider, config.model, self.settings)
        self.vision_provider = build_text_provider(
            config.vision_provider, config.vision_model, self.settings
        )
        self.image_provider = build_image_provider(self.settings)
        self._degraded.clear()
        log.info(
            "AI providers reloaded: text=%s/%s vision=%s/%s images=%s/%s",
            config.provider, config.model, config.vision_provider, config.vision_model,
            self.settings.settings.image_ai.provider, self.settings.settings.image_ai.model,
        )

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Execute a coroutine from synchronous code."""
        return self._loop.run(coro, timeout or self.settings.settings.ai.timeout_seconds + 30)

    def _complete(self, request: TextRequest, *, purpose: str) -> AIResponse:
        """Call the text provider, degrading to the offline analyser on failure."""
        provider = self.text_provider
        if provider.name in self._degraded:
            provider = self._offline
        try:
            return self.run(provider.generate_text(request))
        except Exception as exc:  # noqa: BLE001
            if provider is self._offline:
                raise
            self._degraded.add(provider.name)
            log.error(
                "AI provider '%s' failed during %s (%s); continuing with the offline analyser",
                provider.name, purpose, exc,
            )
            return self.run(self._offline.generate_text(request))

    # ----------------------------------------------------------- editorial
    def analyze_articles(
        self,
        articles: list[dict[str, Any]],
        *,
        project_id: int,
        publication_name: str,
        edition_date: str,
        language: str,
        page_count: int,
        design_style: str,
        max_headline_chars: int = 70,
    ) -> EditorialPlan:
        """Score every article and produce the editorial plan for the edition."""
        request = self.prompts.build_request(
            "editorial/analysis",
            json_mode=True,
            temperature=min(0.5, self.settings.settings.ai.temperature),
            publication_name=publication_name,
            edition_date=edition_date,
            language=language,
            page_count=page_count,
            design_style=design_style,
            max_headline_chars=max_headline_chars,
            articles_json=json.dumps(articles, ensure_ascii=False, indent=1),
            _data={"articles": articles, "page_count": page_count, "language": language},
        )
        response = self._complete(request, purpose="editorial analysis")
        try:
            payload = response.json()
        except AppError:
            log.warning("Editorial response was not JSON; re-running offline")
            payload = self.run(self._offline.generate_text(request)).json()
        return self._to_editorial_plan(payload, project_id, response, page_count)

    def _to_editorial_plan(
        self, payload: Any, project_id: int, response: AIResponse, page_count: int
    ) -> EditorialPlan:
        """Validate and coerce a provider payload into an :class:`EditorialPlan`."""
        if not isinstance(payload, dict):
            payload = {}
        analyses: list[ArticleAnalysis] = []
        for item in payload.get("analyses", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                area = item.get("recommended_area", "secondary")
                analyses.append(
                    ArticleAnalysis(
                        article_id=int(item.get("article_id", 0)),
                        importance=_clamp(item.get("importance", 50)),
                        urgency=_clamp(item.get("urgency", 50)),
                        public_interest=_clamp(item.get("public_interest", 50)),
                        visual_importance=_clamp(item.get("visual_importance", 50)),
                        category=str(item.get("category", "general")),
                        recommended_page=max(1, min(page_count, int(item.get("recommended_page", 1) or 1))),
                        recommended_area=AreaKind(area) if area in {a.value for a in AreaKind} else AreaKind.SECONDARY,
                        headline=str(item.get("headline", "")),
                        subtitle=str(item.get("subtitle", "")),
                        lead=str(item.get("lead", "")),
                        summary=str(item.get("summary", "")),
                        keywords=[str(k) for k in (item.get("keywords") or [])][:12],
                        image_required=bool(item.get("image_required", False)),
                        ai_image_prompt=str(item.get("ai_image_prompt", "")),
                        rationale=str(item.get("rationale", "")),
                    )
                )
            except (ValueError, TypeError) as exc:
                log.warning("Skipping malformed analysis entry: %s", exc)

        assignments: dict[int, list[int]] = {}
        for key, value in (payload.get("page_assignments") or {}).items():
            try:
                page = int(key)
            except (TypeError, ValueError):
                continue
            if 1 <= page <= page_count:
                assignments[page] = [int(v) for v in value if str(v).lstrip("-").isdigit()]

        lead = payload.get("front_page_lead")
        return EditorialPlan(
            project_id=project_id,
            analyses=analyses,
            page_assignments=assignments,
            front_page_lead=int(lead) if isinstance(lead, int | str) and str(lead).isdigit() else None,
            notes=[str(n) for n in (payload.get("notes") or [])],
            provider=response.provider,
            model=response.model,
        )

    def headline(self, title: str, body: str, language: str, max_chars: int = 70) -> dict[str, Any]:
        """Generate a headline, deck and alternatives for one article."""
        request = self.prompts.build_request(
            "editorial/headline",
            json_mode=True,
            temperature=0.6,
            language=language,
            max_chars=max_chars,
            max_subtitle_chars=max_chars + 40,
            title=title,
            body=body[:6000],
            _data={"title": title, "body": body, "language": language, "max_chars": max_chars},
        )
        response = self._complete(request, purpose="headline")
        return response.json(required=False) or {"headline": title, "subtitle": "", "alternatives": []}

    def summarize(self, body: str, language: str, max_words: int = 55) -> dict[str, Any]:
        """Generate the lead, summary and keywords for one article."""
        request = self.prompts.build_request(
            "editorial/summary",
            json_mode=True,
            temperature=0.3,
            language=language,
            max_words=max_words,
            body=body[:8000],
            _data={"body": body, "max_words": max_words},
        )
        response = self._complete(request, purpose="summary")
        return response.json(required=False) or {"lead": "", "summary": "", "keywords": []}

    def image_prompt(
        self,
        subject: str,
        category: str,
        design_style: str,
        aspect_ratio: str,
        frame_width_mm: float,
        frame_height_mm: float,
    ) -> dict[str, Any]:
        """Turn a story into an editorial image-generation prompt."""
        request = self.prompts.build_request(
            "image/generate",
            json_mode=True,
            temperature=0.7,
            subject=subject,
            category=category,
            design_style=design_style,
            aspect_ratio=aspect_ratio,
            frame_width_mm=round(frame_width_mm, 1),
            frame_height_mm=round(frame_height_mm, 1),
            _data={"subject": subject, "category": category},
        )
        response = self._complete(request, purpose="image prompt")
        return response.json(required=False) or {
            "prompt": HeuristicProvider.image_prompt(subject, category),
            "negative_prompt": self.settings.settings.image_ai.negative_prompt,
        }

    def layout_proposal(self, context: dict[str, Any]) -> dict[str, Any]:
        """Ask the model for the editorial weighting of one page."""
        request = self.prompts.build_request(
            "layout/plan",
            json_mode=True,
            temperature=0.5,
            _data={"articles": context.get("articles", [])},
            **context,
        )
        response = self._complete(request, purpose="layout proposal")
        return response.json(required=False) or {"strategy": "hierarchical", "slots": []}

    def correction_plan(self, context: dict[str, Any]) -> dict[str, Any]:
        """Ask the model which corrective actions to apply to a failing page."""
        request = self.prompts.build_request(
            "qa/correction", json_mode=True, temperature=0.2, _data=context, **context
        )
        response = self._complete(request, purpose="layout correction")
        return response.json(required=False) or {"actions": [], "expected_gain": 0}

    # -------------------------------------------------------------- vision
    def review_page(self, image_path: Path, context: dict[str, Any]) -> dict[str, Any]:
        """Run the vision model over a rendered page image."""
        prompt = self.prompts.get("vision/page_qa")
        system, user = prompt.render(**context)
        request = VisionRequest(
            image_paths=[Path(image_path)],
            prompt=user,
            system=system,
            json_mode=True,
            temperature=0.1,
        )
        provider = self.vision_provider
        if not provider.supports_vision or provider.name in self._degraded:
            provider = self._offline
        try:
            response = self.run(provider.analyze_image(request))
        except Exception as exc:  # noqa: BLE001
            log.warning("Vision review failed (%s); geometric QA results are used alone", exc)
            return {"score": 0.0, "issues": [], "unavailable": True, "error": str(exc)[:200]}
        return response.json(required=False) or {"score": 0.0, "issues": []}

    # -------------------------------------------------------------- images
    def generate_image(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Generate one picture, degrading to a marked placeholder on failure."""
        try:
            return self.run(
                self.image_provider.generate(request, Path(target)),
                timeout=self.settings.settings.image_ai.timeout_seconds + 60,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Image generation failed for '%s': %s", request.subject, exc)
            return self.run(DisabledImageProvider().generate(request, Path(target)))

    # --------------------------------------------------------- diagnostics
    def health(self) -> list[ProviderHealth]:
        """Check every configured provider (used by System Diagnostics)."""
        results: list[ProviderHealth] = []
        for label, provider in (
            ("text", self.text_provider),
            ("vision", self.vision_provider),
        ):
            try:
                health = self.run(provider.health_check(), timeout=45)
            except Exception as exc:  # noqa: BLE001
                health = ProviderHealth(provider.name, False, str(exc)[:300])
            health.name = f"{label}:{health.name}"
            results.append(health)
        try:
            image_health = self.run(self.image_provider.health_check(), timeout=45)
        except Exception as exc:  # noqa: BLE001
            image_health = ProviderHealth(self.image_provider.name, False, str(exc)[:300])
        image_health.name = f"image:{image_health.name}"
        results.append(image_health)
        return results

    def prompt_versions(self) -> dict[str, str]:
        """Prompt versions used by this service, recorded with each run."""
        return self.prompts.versions()

    def describe(self) -> dict[str, Any]:
        """Summary shown on the AI Settings page."""
        return {
            "text": self.text_provider.describe(),
            "vision": self.vision_provider.describe(),
            "image": {"name": self.image_provider.name, "model": self.image_provider.model},
            "degraded": sorted(self._degraded),
            "prompts": len(self.prompts.keys()),
        }

    def close(self, keep_loop: bool = False) -> None:
        """Close every provider and (optionally) the private loop."""
        for provider in (self.text_provider, self.vision_provider, self.image_provider):
            try:
                self.run(provider.close(), timeout=10)
            except Exception:  # pragma: no cover
                pass
        if not keep_loop and self._owns_loop:
            self._loop.stop()

    shutdown = close


def _clamp(value: Any, low: int = 0, high: int = 100) -> int:
    """Coerce *value* into an integer inside ``[low, high]``."""
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return (low + high) // 2
