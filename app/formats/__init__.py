"""Formats: every size the studio can produce, named or given by measurement."""

from app.formats.catalog import Format, Medium, SafeArea, Unit
from app.formats.presets import ALL_FORMATS, PRINT_FORMATS, SOCIAL_FORMATS, VIDEO_FORMATS
from app.formats.registry import FORMATS, FormatRegistry, UnknownFormatError

__all__ = [
    "ALL_FORMATS",
    "FORMATS",
    "PRINT_FORMATS",
    "SOCIAL_FORMATS",
    "VIDEO_FORMATS",
    "Format",
    "FormatRegistry",
    "Medium",
    "SafeArea",
    "USER_UNITS",
    "UnknownFormatError",
    "Unit",
]

#: Units the operator may type a size in.
USER_UNITS = [unit.value for unit in Unit]
