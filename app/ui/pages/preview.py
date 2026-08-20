"""Preview page: rendered pages with their QA findings."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.models.schemas import LayoutPlan
from app.ui.pages.base import Page
from app.ui.widgets.common import DataTable, ImageCanvas, Toolbar, run_guarded
from app.vision.renderer import PreviewRenderer

log = logging.getLogger(__name__)


class PreviewPage(Page):
    """Look at each page and the issues QA reported for it."""

    title = "Preview"
    subtitle = "Rendered pages and the quality findings behind their score."
    icon = "◉"

    def build(self) -> None:
        """Create the page list, the canvas and the issue table."""
        toolbar = Toolbar()
        self.render_button = toolbar.add(QPushButton("Render all pages"))
        self.render_button.clicked.connect(self._render_all)
        self.zoom_in = toolbar.add(QPushButton("Zoom in"))
        self.zoom_in.clicked.connect(lambda: self.canvas.zoom_in())
        self.zoom_out = toolbar.add(QPushButton("Zoom out"))
        self.zoom_out.clicked.connect(lambda: self.canvas.zoom_out())
        self.fit_button = toolbar.add(QPushButton("Fit"))
        self.fit_button.clicked.connect(lambda: self.canvas.fit())
        toolbar.stretch()
        self.open_button = toolbar.add(QPushButton("Open the previews folder"))
        self.open_button.clicked.connect(self._open_folder)
        self.root.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.page_list = QListWidget()
        self.page_list.currentRowChanged.connect(self._show_page)
        self.page_list.setMaximumWidth(230)
        left_layout.addWidget(self.page_list)
        splitter.addWidget(left)

        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = ImageCanvas()
        centre_layout.addWidget(self.canvas, 1)
        self.caption = QLabel("")
        self.caption.setObjectName("Subtitle")
        centre_layout.addWidget(self.caption)
        splitter.addWidget(centre)

        self.issues = DataTable(["Severity", "Type", "Frame", "Message", "Found by"])
        self.issues.setMaximumWidth(430)
        splitter.addWidget(self.issues)
        splitter.setStretchFactor(1, 3)
        self.root.addWidget(splitter, 1)

        self.plan: LayoutPlan | None = None
        self.bridge.preview_ready.connect(self._on_preview_ready)
        self.bridge.qa_report.connect(lambda _p: self.refresh())

    def refresh(self) -> None:
        """Reload the plan and the page list."""
        handle = self.handle
        self.page_list.clear()
        if handle is None or not handle.layout_plan_path.exists():
            self.canvas.clear("No pages to preview yet")
            self.issues.fill([])
            self.caption.setText("Run 'Generate Newspaper' to produce pages.")
            return
        self.plan = LayoutPlan.load(handle.layout_plan_path)
        for page in self.plan.pages:
            label = f"Page {page.index}"
            if page.section:
                label += f"  ·  {page.section}"
            label += f"   [{page.qa_score or page.score:.0f}]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, page.index)
            self.page_list.addItem(item)
        if self.page_list.count():
            self.page_list.setCurrentRow(0)

    # --------------------------------------------------------------- pages
    def _show_page(self, row: int) -> None:
        if row < 0 or self.plan is None or self.handle is None:
            return
        item = self.page_list.item(row)
        page_index = int(item.data(Qt.ItemDataRole.UserRole))
        page = self.plan.page(page_index)
        if page is None:
            return

        preview = self._find_preview(page_index)
        if preview is None:
            preview = self._render_page(page_index)
        if preview is not None:
            self.canvas.load(preview)
            self.caption.setText(
                f"Page {page_index} - plan score {page.score:.1f}, QA score {page.qa_score:.1f}, "
                f"{page.iterations} correction iteration(s) - {Path(preview).name}"
            )
        else:
            self.canvas.clear("This page has not been rendered yet")

        issues = page.meta.get("violations") or []
        qa_meta = page.meta.get("qa") or {}
        self.issues.fill(
            [
                [
                    issue.get("severity", ""),
                    issue.get("type", ""),
                    issue.get("element_id", "") or "",
                    issue.get("message", ""),
                    "geometry",
                ]
                for issue in issues
            ]
        )
        if qa_meta:
            self.caption.setText(
                self.caption.text() + f" - analysed by {', '.join(qa_meta.get('analyzed_by', []))}"
            )

    def _find_preview(self, page_index: int) -> Path | None:
        if self.handle is None:
            return None
        candidates = sorted(
            self.handle.previews_dir.glob(f"page_{page_index:03d}_it*.png"),
            key=lambda p: p.stat().st_mtime,
        )
        candidates += sorted(
            self.handle.output_dir.glob(f"previews/page_{page_index:03d}.*"),
            key=lambda p: p.stat().st_mtime,
        )
        return candidates[-1] if candidates else None

    def _render_page(self, page_index: int) -> Path | None:
        if self.plan is None or self.handle is None:
            return None
        page = self.plan.page(page_index)
        if page is None:
            return None
        template = self.app.templates.get_or_default(self.plan.template_id)
        renderer = PreviewRenderer(template, dpi=self.app.settings.settings.export.preview_dpi)
        target = self.handle.previews_dir / f"preview_page_{page_index:03d}.png"
        result = renderer.render_page(page, target)
        return result.path

    def _render_all(self) -> None:
        if self.plan is None or self.handle is None:
            return

        def action() -> None:
            template = self.app.templates.get_or_default(self.plan.template_id)
            renderer = PreviewRenderer(template, dpi=self.app.settings.settings.export.preview_dpi)
            results = renderer.render_plan(self.plan, self.handle.previews_dir, prefix="preview_page")
            self.caption.setText(f"Rendered {len(results)} page(s).")
            self._show_page(self.page_list.currentRow())

        run_guarded(self, "Render pages", action)

    def _on_preview_ready(self, payload: dict) -> None:
        path = payload.get("path")
        if not path:
            return
        current = self.page_list.currentItem()
        if current and int(current.data(Qt.ItemDataRole.UserRole)) == int(payload.get("page", -1)):
            self.canvas.load(path)

    def _open_folder(self) -> None:
        if self.handle is None:
            return
        _open_path(self.handle.previews_dir)


def _open_path(path: Path) -> None:
    """Open a folder or file with the desktop's default handler."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
