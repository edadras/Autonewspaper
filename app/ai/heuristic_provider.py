"""Offline rule-based provider.

This is a real analyser, not a stub. It implements the same interface as the
cloud providers using deterministic linguistic heuristics: news-value scoring
from lexical signals, extractive headline/lead/summary generation, keyword
based categorisation and measurement-driven image description.

It exists for three reasons:

* the application must be usable, end to end, with no API key and no network;
* it is the fallback when a configured provider fails mid-pipeline, so a run
  never dies because a vendor returned 503;
* it makes the editorial stage deterministic in tests.

Structured calls are dispatched on ``TextRequest.metadata["task"]``, which the
:mod:`app.ai.prompts` layer always sets. Cloud providers ignore that metadata.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from app.ai.base import AIProvider, AIResponse, ProviderHealth, TextRequest, VisionRequest
from app.utils import text as T

log = logging.getLogger(__name__)

# --- news-value lexicon -----------------------------------------------------
# Weights are additive contributions to the 0..100 importance score.
URGENCY_TERMS = {
    "فوری": 30,
    "اضطراری": 28,
    "لحظاتی پیش": 22,
    "هم‌اکنون": 20,
    "زنده": 16,
    "breaking": 30,
    "urgent": 26,
    "live": 16,
    "just in": 22,
}
IMPACT_TERMS = {
    "زلزله": 30,
    "سیل": 28,
    "جنگ": 30,
    "حمله": 26,
    "انفجار": 28,
    "بحران": 22,
    "تحریم": 20,
    "انتخابات": 24,
    "دولت": 14,
    "مجلس": 14,
    "رئیس‌جمهور": 20,
    "وزیر": 12,
    "بودجه": 14,
    "تورم": 18,
    "ارز": 14,
    "بورس": 12,
    "کرونا": 18,
    "قتل": 20,
    "تصادف": 14,
    "آتش‌سوزی": 20,
    "اعتصاب": 16,
    "توافق": 14,
    "earthquake": 30,
    "flood": 28,
    "war": 30,
    "attack": 26,
    "explosion": 28,
    "crisis": 22,
    "election": 24,
    "government": 14,
    "president": 20,
    "inflation": 18,
    "sanction": 20,
    "strike": 16,
    "agreement": 14,
    "pandemic": 18,
}
HUMAN_INTEREST_TERMS = {
    "مردم": 12,
    "خانواده": 10,
    "کودکان": 14,
    "دانش‌آموزان": 12,
    "بیمار": 12,
    "کارگر": 10,
    "معلم": 10,
    "شهروندان": 10,
    "زندگی": 8,
    "people": 12,
    "family": 10,
    "children": 14,
    "students": 12,
    "patients": 12,
}
VISUAL_TERMS = {
    "تصویر": 14,
    "عکس": 14,
    "مراسم": 12,
    "جشنواره": 14,
    "نمایشگاه": 14,
    "مسابقه": 12,
    "بازی": 12,
    "افتتاح": 10,
    "راهپیمایی": 14,
    "تخریب": 14,
    "ceremony": 12,
    "festival": 14,
    "exhibition": 14,
    "match": 12,
    "parade": 14,
}

CATEGORY_LEXICON: dict[str, set[str]] = {
    "politics": {
        "دولت",
        "مجلس",
        "انتخابات",
        "رئیس‌جمهور",
        "وزیر",
        "سیاست",
        "نماینده",
        "حزب",
        "قانون",
        "تحریم",
        "دیپلماسی",
        "مذاکره",
        "government",
        "election",
        "parliament",
        "policy",
        "minister",
        "diplomacy",
        "sanction",
    },
    "economy": {
        "اقتصاد",
        "بورس",
        "ارز",
        "دلار",
        "تورم",
        "بازار",
        "بانک",
        "قیمت",
        "بودجه",
        "صادرات",
        "واردات",
        "تولید",
        "مالیات",
        "economy",
        "market",
        "inflation",
        "bank",
        "budget",
        "export",
        "import",
        "tax",
        "price",
    },
    "sport": {
        "فوتبال",
        "تیم",
        "بازیکن",
        "مسابقه",
        "لیگ",
        "قهرمانی",
        "ورزش",
        "المپیک",
        "استقلال",
        "پرسپولیس",
        "گل",
        "football",
        "team",
        "league",
        "match",
        "championship",
        "olympic",
        "player",
    },
    "culture": {
        "فرهنگ",
        "هنر",
        "سینما",
        "فیلم",
        "کتاب",
        "موسیقی",
        "تئاتر",
        "نمایشگاه",
        "جشنواره",
        "نویسنده",
        "culture",
        "art",
        "cinema",
        "film",
        "book",
        "music",
        "theatre",
        "festival",
    },
    "society": {
        "جامعه",
        "شهروندان",
        "شهرداری",
        "ترافیک",
        "آموزش",
        "مدرسه",
        "دانشگاه",
        "بهداشت",
        "بیمارستان",
        "محیط زیست",
        "society",
        "city",
        "traffic",
        "education",
        "school",
        "university",
        "health",
        "environment",
    },
    "world": {
        "جهان",
        "بین‌الملل",
        "آمریکا",
        "اروپا",
        "چین",
        "روسیه",
        "سازمان ملل",
        "world",
        "international",
        "global",
        "europe",
        "china",
        "russia",
        "united nations",
    },
    "science": {
        "علم",
        "فناوری",
        "پژوهش",
        "دانشمند",
        "هوش مصنوعی",
        "فضا",
        "ناسا",
        "science",
        "technology",
        "research",
        "artificial intelligence",
        "space",
        "nasa",
    },
    "incident": {
        "حادثه",
        "تصادف",
        "آتش‌سوزی",
        "زلزله",
        "سیل",
        "انفجار",
        "قتل",
        "سرقت",
        "accident",
        "fire",
        "earthquake",
        "flood",
        "explosion",
        "crime",
    },
}


def _score_terms(text: str, lexicon: dict[str, int]) -> int:
    lowered = text.lower()
    return sum(weight for term, weight in lexicon.items() if term.lower() in lowered)


class HeuristicProvider(AIProvider):
    """Deterministic offline editorial analyser."""

    name = "heuristic"
    supports_vision = True
    supports_json_mode = True
    requires_network = False

    def __init__(self, model: str = "rule-based-v1", **options: Any) -> None:
        super().__init__(model, **options)

    # ------------------------------------------------------------- text ---
    async def generate_text(self, request: TextRequest) -> AIResponse:
        """Dispatch on the structured task carried in ``request.metadata``."""
        started = time.monotonic()
        task = str(request.metadata.get("task", ""))
        data = request.metadata.get("data") or {}
        handler = {
            "editorial_analysis": self._editorial_analysis,
            "headline": self._headline,
            "summary": self._summary,
            "layout_proposal": self._layout_proposal,
            "qa_review": self._qa_review,
            "image_prompt": self._image_prompt,
        }.get(task)
        if handler is None:
            payload: Any = {"text": self._fallback_text(request)}
        else:
            payload = handler(data)
        text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        return AIResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_seconds=time.monotonic() - started,
            raw={"task": task},
        )

    def _fallback_text(self, request: TextRequest) -> str:
        """Echo the last user turn, condensed. Used for free-form prompts."""
        user_turns = [m.content for m in request.messages if m.role == "user"]
        return T.summarize(user_turns[-1] if user_turns else "", 60)

    # -------------------------------------------------------- editorial ---
    def _editorial_analysis(self, data: dict[str, Any]) -> dict[str, Any]:
        """Score and plan every article handed in ``data['articles']``."""
        articles: list[dict[str, Any]] = data.get("articles", [])
        page_count = int(data.get("page_count", 8) or 8)
        language = data.get("language", "fa")
        analyses = [self.analyze_article(article, language) for article in articles]
        analyses.sort(key=lambda a: -a["priority"])
        assignments = self._assign_pages(analyses, page_count)
        for analysis in analyses:
            analysis["recommended_page"] = next(
                (page for page, ids in assignments.items() if analysis["article_id"] in ids), 1
            )
            analysis["recommended_area"] = self._area_for(analysis, assignments)
        return {
            "analyses": analyses,
            "page_assignments": {str(k): v for k, v in assignments.items()},
            "front_page_lead": analyses[0]["article_id"] if analyses else None,
            "notes": [
                "Scored offline with the rule-based editorial model "
                "(news value, urgency, human interest, visual potential)."
            ],
        }

    def analyze_article(self, article: dict[str, Any], language: str = "fa") -> dict[str, Any]:
        """Full editorial analysis of a single article dictionary."""
        title = str(article.get("title") or "")
        body = str(article.get("body") or "")
        haystack = f"{title}\n{title}\n{body[:4000]}"
        words = T.word_count(body)

        impact = _score_terms(haystack, IMPACT_TERMS)
        urgency = min(100, int(round(30 + _score_terms(haystack, URGENCY_TERMS) + impact * 0.30)))
        interest = _score_terms(haystack, HUMAN_INTEREST_TERMS)
        visual = _score_terms(haystack, VISUAL_TERMS)

        length_bonus = min(18.0, 6.0 * math.log1p(words / 60.0))
        importance = min(100, int(round(35 + impact * 1.1 + length_bonus)))
        public_interest = min(100, int(round(32 + interest * 1.4 + impact * 0.35)))
        visual_importance = min(
            100,
            int(round(28 + visual * 1.5 + (18 if article.get("has_image") else 0) + impact * 0.2)),
        )
        if article.get("importance_override") is not None:
            importance = int(article["importance_override"])

        headline = self.make_headline(title, body, language)
        lead = T.lead_paragraph(body, 32) or T.truncate_words(body, 32)
        summary = T.summarize(body, 55)
        keywords = T.keywords(f"{title} {body}", 8)
        category = self.categorize(f"{title} {body}", title)

        priority = int(
            round(0.40 * importance + 0.25 * urgency + 0.25 * public_interest + 0.10 * visual_importance)
        )
        return {
            "article_id": int(article.get("id", 0)),
            "importance": importance,
            "urgency": urgency,
            "public_interest": public_interest,
            "visual_importance": visual_importance,
            "priority": priority,
            "category": category,
            "headline": headline,
            "subtitle": self.make_subtitle(body, language),
            "lead": lead,
            "summary": summary,
            "keywords": keywords,
            "image_required": visual_importance >= 55 or priority >= 70,
            "ai_image_prompt": self.image_prompt(headline or title, category, language),
            "recommended_page": 1,
            "recommended_area": "secondary",
            "rationale": (
                f"impact={impact} urgency={urgency} interest={interest} visual={visual} words={words}"
            ),
        }

    @staticmethod
    def categorize(text: str, title: str = "") -> str:
        """Pick the best matching category from the lexicon.

        Matches are counted rather than merely detected, terms occurring in the
        title weigh three times as much, and longer (more specific) terms weigh
        more than short generic ones - so a story mentioning "rescue teams"
        once does not get filed under sport because it contains "team".
        """
        body = text.lower()
        head = (title or "").lower()
        scores: dict[str, float] = {}
        for name, terms in CATEGORY_LEXICON.items():
            total = 0.0
            for term in terms:
                needle = term.lower()
                specificity = 1.0 + 0.25 * len(needle.split()) + min(0.6, len(needle) / 20.0)
                total += body.count(needle) * specificity
                total += head.count(needle) * specificity * 3.0
            scores[name] = total
        best, score = max(scores.items(), key=lambda kv: (kv[1], -len(kv[0])))
        return best if score > 0 else "general"

    @staticmethod
    def make_headline(title: str, body: str, language: str = "fa") -> str:
        """Return a tightened headline, derived from the body when absent."""
        if title.strip():
            return T.clean_headline(title, language, 70)
        first = (T.sentences(body) or [""])[0]
        # Drop a leading dateline such as "تهران - " or "TEHRAN, Aug 20 -".
        first = re.sub(r"^[^-–—]{0,40}[-–—]\s*", "", first)
        return T.clean_headline(first, language, 70)

    @staticmethod
    def make_subtitle(body: str, language: str = "fa") -> str:
        """Second-sentence subtitle, capped for a two-line deck."""
        parts = T.sentences(body)
        if len(parts) < 2:
            return ""
        return T.clean_headline(parts[1], language, 110)

    @staticmethod
    def image_prompt(subject: str, category: str, language: str = "fa") -> str:
        """Build an editorial image prompt for a story."""
        scene = {
            "politics": "official press conference, formal setting, flags in background",
            "economy": "financial district, trading floor, currency and market imagery",
            "sport": "stadium action shot, motion blur, crowd in background",
            "culture": "cultural venue, warm lighting, audience",
            "society": "urban street scene, everyday life, documentary framing",
            "world": "international summit, diplomatic setting",
            "science": "modern laboratory, researchers at work, clean lighting",
            "incident": "emergency response scene, rescue workers, dramatic natural light",
        }.get(category, "editorial news scene, documentary framing")
        return (
            f"{subject}. {scene}. Realistic editorial press photography, natural lighting, "
            f"35mm lens, shallow depth of field, no text, no watermark, no logo."
        )

    def _assign_pages(self, analyses: list[dict[str, Any]], page_count: int) -> dict[int, list[int]]:
        """Distribute stories over the edition.

        Explicit page preferences win. The front page is then filled from the
        top of the priority ranking (it is never left empty), and the rest of
        the edition is grouped by category so sections read coherently.
        """
        page_count = max(1, page_count)
        pages: dict[int, list[int]] = {index: [] for index in range(1, page_count + 1)}
        capacity = {index: (3 if index == 1 else 5) for index in pages}
        remaining: list[dict[str, Any]] = []

        for analysis in analyses:
            preferred = analysis.get("page_preference")
            if preferred and 1 <= int(preferred) <= page_count:
                pages[int(preferred)].append(analysis["article_id"])
            else:
                remaining.append(analysis)

        remaining.sort(key=lambda a: -a["priority"])
        front_slots = max(0, capacity[1] - len(pages[1]))
        for analysis in remaining[:front_slots]:
            pages[1].append(analysis["article_id"])
        remaining = remaining[front_slots:]

        # Distribute the rest so every page carries a comparable amount of
        # copy, while keeping a category on one page where that still balances.
        category_pages: dict[str, int] = {}
        load: dict[int, float] = {index: 0.0 for index in pages}
        for page_index, ids in pages.items():
            load[page_index] = float(len(ids))

        def demand(analysis: dict[str, Any]) -> float:
            words = analysis.get("word_count") or 0
            return 1.0 + min(4.0, words / 250.0)

        for analysis in remaining:
            category = analysis["category"]
            options = (
                [p for p in pages if p > 1 and len(pages[p]) < capacity[p]]
                or [p for p in pages if p > 1]
                or [1]
            )
            preferred = category_pages.get(category)

            def cost(page_index: int, _preferred: int | None = preferred) -> tuple[float, int]:
                penalty = load[page_index]
                if _preferred == page_index:
                    penalty -= 1.2
                elif _preferred is None and not pages[page_index]:
                    penalty -= 0.4
                return (penalty, page_index)

            page = min(options, key=cost)
            pages[page].append(analysis["article_id"])
            load[page] += demand(analysis)
            category_pages.setdefault(category, page)
        return pages

    @staticmethod
    def _area_for(analysis: dict[str, Any], assignments: dict[int, list[int]]) -> str:
        page = analysis["recommended_page"]
        ids = assignments.get(page, [])
        if not ids:
            return "secondary"
        position = ids.index(analysis["article_id"]) if analysis["article_id"] in ids else len(ids)
        if position == 0:
            return "main"
        if position == 1:
            return "secondary"
        if position >= 3:
            return "sidebar" if analysis["priority"] < 40 else "small"
        return "small"

    # ---------------------------------------------------- small helpers ---
    def _headline(self, data: dict[str, Any]) -> dict[str, Any]:
        title = str(data.get("title", ""))
        body = str(data.get("body", ""))
        language = str(data.get("language", "fa"))
        limit = int(data.get("max_chars", 70))
        headline = T.truncate_chars(self.make_headline(title, body, language), limit, "")
        return {
            "headline": headline,
            "subtitle": self.make_subtitle(body, language),
            "alternatives": [
                T.truncate_chars(headline, max(18, limit // 2), ""),
                T.clean_headline(T.lead_paragraph(body, 9), language, limit),
            ],
        }

    def _summary(self, data: dict[str, Any]) -> dict[str, Any]:
        body = str(data.get("body", ""))
        return {
            "summary": T.summarize(body, int(data.get("max_words", 55))),
            "lead": T.lead_paragraph(body, 32),
            "keywords": T.keywords(body, 8),
        }

    def _image_prompt(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "prompt": self.image_prompt(
                str(data.get("subject", "")),
                str(data.get("category", "general")),
                str(data.get("language", "fa")),
            ),
            "negative_prompt": "text, watermark, logo, caption, signature, distorted faces",
        }

    def _layout_proposal(self, data: dict[str, Any]) -> dict[str, Any]:
        """Suggest slot weights; the geometric engine does the real work."""
        articles = data.get("articles", [])
        total = max(1, len(articles))
        return {
            "strategy": "hierarchical",
            "slots": [
                {
                    "article_id": article.get("id"),
                    "weight": round(max(0.08, (article.get("priority", 50) / 100.0) / total * 3.2), 3),
                    "area": "main" if index == 0 else ("secondary" if index < 3 else "small"),
                }
                for index, article in enumerate(articles)
            ],
        }

    def _qa_review(self, data: dict[str, Any]) -> dict[str, Any]:
        """Restate the measured metrics; pixel/geometry QA is authoritative."""
        metrics = data.get("metrics", {})
        issues = data.get("issues", [])
        return {
            "score": float(data.get("score", 0.0)),
            "issues": issues,
            "comments": [f"{key}={value}" for key, value in sorted(metrics.items())],
        }

    # ----------------------------------------------------------- vision ---
    async def analyze_image(self, request: VisionRequest) -> AIResponse:
        """Describe images from measured statistics (no model involved)."""
        from app.utils import imaging

        started = time.monotonic()
        findings = []
        for path in request.image_paths:
            analysis = imaging.analyze(Path(path))
            findings.append(
                {
                    "path": str(path),
                    "width": analysis.width,
                    "height": analysis.height,
                    "orientation": analysis.orientation,
                    "sharpness": analysis.sharpness,
                    "brightness": analysis.brightness,
                    "contrast": analysis.contrast,
                    "colorfulness": analysis.colorfulness,
                    "faces": analysis.face_count,
                    "quality_score": analysis.quality_score,
                    "problems": analysis.problems,
                }
            )
        payload = {
            "score": round(sum(f["quality_score"] for f in findings) / len(findings), 2) if findings else 0.0,
            "issues": [],
            "images": findings,
            "note": "Measured with the local image analyser; no vision model configured.",
        }
        return AIResponse(
            text=json.dumps(payload, ensure_ascii=False),
            provider=self.name,
            model=self.model,
            latency_seconds=time.monotonic() - started,
        )

    async def health_check(self) -> ProviderHealth:
        """Always available - it runs in-process."""
        return ProviderHealth(
            self.name, True, "Offline rule-based analyser (no network required)", [self.model], 0.0
        )
