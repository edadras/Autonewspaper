"""Reading a design system back out of a publication that already exists.

A newspaper that has been coming out for years is a specification nobody
wrote down. This package measures one - its sheet, its margins, its column
grid, its colours, its type scale and the boxes its pages are built from -
so the studio can work in that paper's own idiom rather than in a house
style of its own invention.
"""

from app.harvest.publication import (
    DesignSystem,
    HarvestedPage,
    Panel,
    Rule,
    TypeBand,
)
from app.harvest.reader import PageRaster, read_images, read_pdf

__all__ = [
    "DesignSystem",
    "HarvestedPage",
    "PageRaster",
    "Panel",
    "Rule",
    "TypeBand",
    "read_images",
    "read_pdf",
]
