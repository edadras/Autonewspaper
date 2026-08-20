"""Rendering, page analysis, Vision QA and the correction loop."""

from app.vision.analyzer import PageAnalyzer, PixelMetrics, score_from_issues
from app.vision.corrector import CorrectionAction, CorrectionResult, LayoutCorrector
from app.vision.fonts import check_fonts, load_font, resolve_font_file
from app.vision.qa_agent import LoopResult, VisionQAAgent
from app.vision.renderer import PreviewRenderer, RenderResult, shaping_available, shaping_engine

__all__ = [
    "PreviewRenderer",
    "RenderResult",
    "shaping_available",
    "shaping_engine",
    "PageAnalyzer",
    "PixelMetrics",
    "score_from_issues",
    "LayoutCorrector",
    "CorrectionAction",
    "CorrectionResult",
    "VisionQAAgent",
    "LoopResult",
    "resolve_font_file",
    "load_font",
    "check_fonts",
]
