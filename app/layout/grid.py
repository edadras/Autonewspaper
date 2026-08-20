"""The column / baseline grid.

Every frame the engine produces is snapped to this grid: horizontally to
column boundaries, vertically to the baseline grid. Snapping is what makes an
automatically generated page look designed rather than computed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import Rect
from app.templates.schema import TemplateSpec


@dataclass
class GridSystem:
    """Column and baseline grid for one page."""

    content: Rect
    columns: int
    gutter_mm: float
    baseline_mm: float
    rows: int = 12
    direction: str = "rtl"

    @classmethod
    def from_template(cls, template: TemplateSpec, page_index: int = 1) -> GridSystem:
        """Build the grid for *page_index* of *template*."""
        top, bottom, left, right = template.margins_for(page_index)
        content = Rect(
            x=left,
            y=top,
            width=template.page_width_mm - left - right,
            height=template.page_height_mm - top - bottom,
        )
        return cls(
            content=content,
            columns=template.grid.columns,
            gutter_mm=template.grid.gutter_mm,
            baseline_mm=template.grid.baseline_mm,
            rows=template.grid.rows,
            direction=template.direction,
        )

    # ------------------------------------------------------------- columns
    @property
    def column_width(self) -> float:
        """Width of one column."""
        return (self.content.width - self.gutter_mm * (self.columns - 1)) / max(1, self.columns)

    def column_x(self, index: int) -> float:
        """Left edge of column *index* (0-based, always left-to-right).

        Reading direction affects which column is *first* editorially, not
        where it sits geometrically; :meth:`reading_column` converts between
        the two.
        """
        index = max(0, min(self.columns - 1, index))
        return self.content.x + index * (self.column_width + self.gutter_mm)

    def reading_column(self, index: int) -> int:
        """Convert a reading-order column index into a geometric one."""
        if self.direction == "rtl":
            return self.columns - 1 - index
        return index

    def span_width(self, span: int) -> float:
        """Width covered by *span* adjacent columns including their gutters."""
        span = max(1, min(span, self.columns))
        return self.column_width * span + self.gutter_mm * (span - 1)

    def column_span_rect(self, start_column: int, span: int, y: float, height: float) -> Rect:
        """Rectangle covering *span* columns starting at *start_column*."""
        span = max(1, min(span, self.columns))
        start_column = max(0, min(self.columns - span, start_column))
        return Rect(x=self.column_x(start_column), y=y, width=self.span_width(span), height=height)

    def columns_for_width(self, width_mm: float) -> int:
        """How many columns a given width corresponds to (rounded)."""
        unit = self.column_width + self.gutter_mm
        if unit <= 0:
            return 1
        return max(1, min(self.columns, int(round((width_mm + self.gutter_mm) / unit))))

    def nearest_column_index(self, x_mm: float) -> int:
        """Column whose left edge is closest to *x_mm*."""
        unit = self.column_width + self.gutter_mm
        if unit <= 0:
            return 0
        return max(0, min(self.columns - 1, int(round((x_mm - self.content.x) / unit))))

    # ------------------------------------------------------------ snapping
    def snap_rect(self, rect: Rect, *, snap_baseline: bool = True) -> Rect:
        """Snap a rectangle to the column grid (and optionally the baseline)."""
        start = self.nearest_column_index(rect.x)
        span = self.columns_for_width(rect.width)
        span = max(1, min(span, self.columns - start))
        x = self.column_x(start)
        width = self.span_width(span)

        y, height = rect.y, rect.height
        if snap_baseline and self.baseline_mm > 0:
            offset = self.content.y
            y = offset + round((rect.y - offset) / self.baseline_mm) * self.baseline_mm
            height = max(self.baseline_mm, round(rect.height / self.baseline_mm) * self.baseline_mm)

        snapped = Rect(x=x, y=y, width=width, height=height)
        return self.clamp(snapped)

    def snap_y(self, value: float) -> float:
        """Snap a vertical coordinate to the baseline grid."""
        if self.baseline_mm <= 0:
            return value
        return self.content.y + round((value - self.content.y) / self.baseline_mm) * self.baseline_mm

    def clamp(self, rect: Rect) -> Rect:
        """Force a rectangle inside the live area."""
        width = min(rect.width, self.content.width)
        height = min(rect.height, self.content.height)
        x = min(max(rect.x, self.content.x), self.content.right - width)
        y = min(max(rect.y, self.content.y), self.content.bottom - height)
        return Rect(x=round(x, 3), y=round(y, 3), width=round(width, 3), height=round(height, 3))

    # --------------------------------------------------------------- rows
    @property
    def row_height(self) -> float:
        """Height of one horizontal module."""
        return self.content.height / max(1, self.rows)

    def module_rect(self, column: int, row: int, span: int = 1, row_span: int = 1) -> Rect:
        """Rectangle covering a block of grid modules."""
        return self.clamp(
            Rect(
                x=self.column_x(column),
                y=self.content.y + row * self.row_height,
                width=self.span_width(span),
                height=row_span * self.row_height,
            )
        )

    def describe(self) -> dict[str, float | int]:
        """Numbers shown in the layout inspector."""
        return {
            "columns": self.columns,
            "column_width_mm": round(self.column_width, 2),
            "gutter_mm": self.gutter_mm,
            "baseline_mm": self.baseline_mm,
            "rows": self.rows,
            "row_height_mm": round(self.row_height, 2),
            "content_width_mm": round(self.content.width, 2),
            "content_height_mm": round(self.content.height, 2),
        }
