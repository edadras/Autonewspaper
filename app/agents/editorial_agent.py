"""The editorial agent.

Wraps the AI service's editorial operations with the rule of specification §8:
the body of a story is never rewritten without the operator asking for it.
Headlines, decks, leads and summaries are AI-editable; the copy is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.ai.registry import AIService
from app.models import entities as E  # noqa: N812
from app.models.schemas import EditorialPlan
from app.services.project_manager import ProjectHandle
from app.utils import text as T

log = logging.getLogger(__name__)

#: Fields the agent may change on its own.
AI_EDITABLE = ("title", "subtitle", "lead", "summary")


@dataclass
class HeadlineSuggestion:
    """A proposed headline and its alternatives."""

    headline: str
    subtitle: str = ""
    alternatives: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.alternatives is None:
            self.alternatives = []

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "headline": self.headline,
            "subtitle": self.subtitle,
            "alternatives": self.alternatives,
        }


class EditorialAgent:
    """Editorial operations the UI and the pipeline share."""

    def __init__(self, ai: AIService) -> None:
        self.ai = ai

    # ------------------------------------------------------------ edition
    def plan_edition(
        self,
        handle: ProjectHandle,
        *,
        page_count: int | None = None,
        max_headline_chars: int = 70,
    ) -> EditorialPlan:
        """Score every story of a project and assign it to a page."""
        project = handle.project()
        with handle.uow() as uow:
            articles = uow.articles.for_project(handle.project_id)
            payload = [
                {
                    "id": article.id,
                    "title": article.display_title,
                    "body": article.body[:6000],
                    "category": article.category,
                    "author": article.author,
                    "source": article.source,
                    "word_count": article.word_count,
                    "page_preference": article.page_preference,
                    "has_image": bool(uow.assets.for_article(article.id)),
                }
                for article in articles
            ]
        return self.ai.analyze_articles(
            payload,
            project_id=handle.project_id,
            publication_name=project["publication_name"],
            edition_date=str(project["edition_date"] or ""),
            language=project["language"],
            page_count=page_count or int(project["page_count"]),
            design_style=project["design_style"],
            max_headline_chars=max_headline_chars,
        )

    # ------------------------------------------------------------ article
    def suggest_headline(
        self, handle: ProjectHandle, article_id: int, max_chars: int = 70
    ) -> HeadlineSuggestion:
        """Propose a headline without applying it."""
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None:
                raise ValueError(f"Article {article_id} does not exist")
            title, body, language = article.display_title, article.body, article.language
        payload = self.ai.headline(title, body, language, max_chars)
        return HeadlineSuggestion(
            headline=T.clean_headline(str(payload.get("headline") or title), language, max_chars),
            subtitle=str(payload.get("subtitle") or ""),
            alternatives=[str(a) for a in (payload.get("alternatives") or [])][:4],
        )

    def apply_headline(
        self, handle: ProjectHandle, article_id: int, suggestion: HeadlineSuggestion
    ) -> dict[str, Any]:
        """Apply a headline the operator accepted."""
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None:
                raise ValueError(f"Article {article_id} does not exist")
            article.original_title = article.original_title or article.title
            article.title = suggestion.headline
            if suggestion.subtitle:
                article.subtitle = suggestion.subtitle
            return article.to_dict()

    def summarize(self, handle: ProjectHandle, article_id: int, max_words: int = 55) -> dict[str, Any]:
        """Generate the lead, summary and keywords for one story."""
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None:
                raise ValueError(f"Article {article_id} does not exist")
            body, language = article.body, article.language
        payload = self.ai.summarize(body, language, max_words)
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            assert article is not None
            if payload.get("lead"):
                article.lead = str(payload["lead"])
            if payload.get("summary"):
                article.summary = str(payload["summary"])
            meta = article.meta
            meta["keywords"] = [str(k) for k in (payload.get("keywords") or [])][:12]
            article.set_meta(meta)
            return article.to_dict()

    def rescore(self, handle: ProjectHandle, article_id: int) -> dict[str, Any]:
        """Re-score a single story after the operator edited it."""
        plan = self.plan_edition(handle)
        analysis = plan.analysis_for(article_id)
        if analysis is None:
            return {}
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None:
                return {}
            article.importance = analysis.importance
            article.urgency = analysis.urgency
            article.public_interest = analysis.public_interest
            article.visual_importance = analysis.visual_importance
            article.priority = analysis.priority
            article.category = analysis.category or article.category
            return article.to_dict()

    # ------------------------------------------------------------- guards
    @staticmethod
    def protected_fields() -> tuple[str, ...]:
        """Fields the agent must never change on its own."""
        return ("body",)

    @staticmethod
    def approve(handle: ProjectHandle, article_id: int, approved: bool = True) -> bool:
        """Mark a story as operator-approved.

        An approved story is left alone by later editorial passes, so a manual
        edit is never overwritten by a regenerated headline.
        """
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None:
                return False
            article.approved = approved
            return True

    @staticmethod
    def restore_original_title(handle: ProjectHandle, article_id: int) -> bool:
        """Put the imported headline back."""
        with handle.uow() as uow:
            article = uow.articles.get(article_id)
            if article is None or not article.original_title:
                return False
            article.title = article.original_title
            return True

    def edition_summary(self, handle: ProjectHandle) -> dict[str, Any]:
        """Counts and the running order, shown on the Content page."""
        with handle.uow() as uow:
            articles = uow.articles.by_priority(handle.project_id)
            return {
                "articles": len(articles),
                "approved": sum(1 for a in articles if a.approved),
                "categories": uow.articles.categories(handle.project_id),
                "front_page": [
                    {"id": a.id, "title": a.display_title, "priority": a.priority}
                    for a in articles
                    if (a.recommended_page or 1) == 1
                ],
                "running_order": [
                    {
                        "id": a.id,
                        "title": a.display_title,
                        "page": a.recommended_page,
                        "area": a.recommended_area,
                        "priority": a.priority,
                        "words": a.word_count,
                    }
                    for a in articles
                ],
            }
