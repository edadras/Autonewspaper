"""Template system: specification, catalogue and validation."""

from app.templates.manager import TemplateManager
from app.templates.schema import (
    ColorSpec,
    GridSpec,
    LayoutRules,
    MarginSpec,
    MasterPageSpec,
    ObjectStyleSpec,
    ParagraphStyleSpec,
    PDFPresetSpec,
    TemplateSpec,
)

__all__ = [
    "TemplateManager",
    "TemplateSpec",
    "MarginSpec",
    "GridSpec",
    "ColorSpec",
    "ParagraphStyleSpec",
    "ObjectStyleSpec",
    "MasterPageSpec",
    "LayoutRules",
    "PDFPresetSpec",
]
