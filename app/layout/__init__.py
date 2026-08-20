"""Constraint-based layout engine."""

from app.layout.constraints import ConstraintChecker, ConstraintReport, Violation
from app.layout.engine import LayoutEngine, PageCandidate, PagePlan, make_image_slot
from app.layout.grid import GridSystem
from app.layout.scoring import LayoutScore, LayoutScorer
from app.layout.strategies import STRATEGIES, ArticleBlock, ImageSlot, Region, build_candidates
from app.layout.typography import FitResult, TypographyEngine

__all__ = [
    "LayoutEngine",
    "PagePlan",
    "PageCandidate",
    "make_image_slot",
    "GridSystem",
    "TypographyEngine",
    "FitResult",
    "ConstraintChecker",
    "ConstraintReport",
    "Violation",
    "LayoutScorer",
    "LayoutScore",
    "ArticleBlock",
    "ImageSlot",
    "Region",
    "STRATEGIES",
    "build_candidates",
]
