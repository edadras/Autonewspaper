"""Layout page: inspect and hand-edit the plan the engine produced."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.undo import Command
from app.layout.engine import LayoutEngine
from app.models.schemas import LayoutPlan, Rect
from app.ui.pages.base import Page
from app.ui.widgets.common import Card, ImageCanvas, Toolbar, run_guarded, show_error
from app.vision.renderer import PreviewRenderer

log = logging.getLogger(__name__)


class LayoutPage(Page):
    """The plan tree, a live preview and manual overrides."""

    title = "Layout"
    subtitle = "Every frame the engine placed, with manual override, undo and re-scoring."
    icon = "▧"

    def build(self) -> None:
        """Create the tree, the inspector and the preview."""
        toolbar = Toolbar()
        self.reload_button = toolbar.add(QPushButton("Reload plan"))
        self.reload_button.clicked.connect(self.refresh)
        self.rescore_button = toolbar.add(QPushButton("Re-score page"))
        self.rescore_button.clicked.connect(self._rescore)
        self.render_button = toolbar.add(QPushButton("Render page"))
        self.render_button.clicked.connect(self._render)
        toolbar.stretch()
        self.undo_button = toolbar.add(QPushButton("Undo"))
        self.undo_button.clicked.connect(self._undo)
        self.redo_button = toolbar.add(QPushButton("Redo"))
        self.redo_button.clicked.connect(self._redo)
        self.save_plan_button = toolbar.add(QPushButton("Save plan"))
        self.save_plan_button.setObjectName("Primary")
        self.save_plan_button.clicked.connect(self._save_plan)
        self.root.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Frame", "Type", "x", "y", "w", "h", "pt", "Overflow"])
        self.tree.itemSelectionChanged.connect(self._select)
        splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = ImageCanvas()
        self.canvas.setMinimumWidth(300)
        right_layout.addWidget(self.canvas, 1)
        right_layout.addWidget(self._inspector())
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.root.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("Subtitle")
        self.status.setWordWrap(True)
        self.root.addWidget(self.status)

        self.plan: LayoutPlan | None = None
        self._engine: LayoutEngine | None = None
        self._renderer: PreviewRenderer | None = None
        self.bridge.layout_planned.connect(lambda _p: self.refresh())
        self.app.undo.subscribe(self._update_undo_buttons)

    def _inspector(self) -> QWidget:
        group = QGroupBox("Selected frame")
        form = QFormLayout(group)
        self.x_spin = QDoubleSpinBox()
        self.y_spin = QDoubleSpinBox()
        self.w_spin = QDoubleSpinBox()
        self.h_spin = QDoubleSpinBox()
        for spin, maximum in (
            (self.x_spin, 2000), (self.y_spin, 2000), (self.w_spin, 2000), (self.h_spin, 2000)
        ):
            spin.setRange(0.0, float(maximum))
            spin.setDecimals(2)
            spin.setSuffix(" mm")
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4.0, 200.0)
        self.size_spin.setSuffix(" pt")
        self.style_box = QComboBox()
        self.text_edit = QPlainTextEdit()
        self.text_edit.setMaximumHeight(90)
        form.addRow("x", self.x_spin)
        form.addRow("y", self.y_spin)
        form.addRow("width", self.w_spin)
        form.addRow("height", self.h_spin)
        form.addRow("type size", self.size_spin)
        form.addRow("style", self.style_box)
        form.addRow("text", self.text_edit)

        buttons = Toolbar()
        self.apply_button = buttons.add(QPushButton("Apply"))
        self.apply_button.clicked.connect(self._apply)
        self.delete_button = buttons.add(QPushButton("Delete frame"))
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete)
        buttons.stretch()
        form.addRow("", buttons)
        return group

    # ------------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Reload the plan from disk."""
        handle = self.handle
        self.tree.clear()
        if handle is None or not handle.layout_plan_path.exists():
            self.plan = None
            self.status.setText(
                "No layout plan yet. Run 'Generate Newspaper' on the Dashboard first."
            )
            self.canvas.clear("No plan")
            return

        def action() -> None:
            self.plan = LayoutPlan.load(handle.layout_plan_path)
            template = self.app.templates.get_or_default(self.plan.template_id)
            self._engine = LayoutEngine(template, language=self.plan.language)
            self._renderer = PreviewRenderer(
                template, dpi=self.app.settings.settings.export.preview_dpi
            )
            self.style_box.clear()
            self.style_box.addItems([style.id for style in template.paragraph_styles])
            self._fill_tree()
            self.status.setText(
                f"{len(self.plan.pages)} page(s), {self.plan.element_count()} frame(s), "
                f"edition score {self.plan.score:.1f}"
            )
            self._update_undo_buttons()

        run_guarded(self, "Load layout plan", action)

    def _fill_tree(self) -> None:
        assert self.plan is not None
        self.tree.clear()
        for page in self.plan.pages:
            page_item = QTreeWidgetItem(
                [
                    f"Page {page.index}"
                    + (f"  ({page.section})" if page.section else "")
                    + ("  [empty]" if page.meta.get("empty") else ""),
                    page.meta.get("strategy", ""),
                    "", "", "", "",
                    "",
                    f"score {page.score:.1f} / QA {page.qa_score:.1f}",
                ]
            )
            page_item.setData(0, Qt.ItemDataRole.UserRole, ("page", page.index))
            for element in sorted(page.elements, key=lambda e: (e.rect.y, e.rect.x)):
                child = QTreeWidgetItem(
                    [
                        element.frame_name + ("  🔒" if element.locked else ""),
                        element.type.value,
                        f"{element.rect.x:.1f}",
                        f"{element.rect.y:.1f}",
                        f"{element.rect.width:.1f}",
                        f"{element.rect.height:.1f}",
                        f"{element.typography.size_pt:.1f}" if element.typography else "",
                        f"{element.estimated_overflow * 100:.0f}%" if element.estimated_overflow else "",
                    ]
                )
                child.setData(0, Qt.ItemDataRole.UserRole, ("element", page.index, element.id))
                page_item.addChild(child)
            self.tree.addTopLevelItem(page_item)
            page_item.setExpanded(page.index == 1)
        for column in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(column)

    # ------------------------------------------------------------ selection
    def _selected(self) -> tuple[int, str] | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return None
        if data[0] == "page":
            return (data[1], "")
        return (data[1], data[2])

    def _select(self) -> None:
        selection = self._selected()
        if selection is None or self.plan is None:
            return
        page_index, element_id = selection
        if not element_id:
            self._render(page_index)
            return
        page = self.plan.page(page_index)
        element = page.element(element_id) if page else None
        if element is None:
            return
        self.x_spin.setValue(element.rect.x)
        self.y_spin.setValue(element.rect.y)
        self.w_spin.setValue(element.rect.width)
        self.h_spin.setValue(element.rect.height)
        self.size_spin.setValue(element.typography.size_pt if element.typography else 9.5)
        index = self.style_box.findText(element.style_id)
        if index >= 0:
            self.style_box.setCurrentIndex(index)
        self.text_edit.setPlainText(element.text)
        for widget in (self.x_spin, self.y_spin, self.w_spin, self.h_spin, self.apply_button,
                       self.delete_button, self.text_edit, self.size_spin):
            widget.setEnabled(not element.locked)

    # --------------------------------------------------------------- edits
    def _apply(self) -> None:
        selection = self._selected()
        if selection is None or self.plan is None or self._engine is None:
            return
        page_index, element_id = selection
        if not element_id:
            return
        page = self.plan.page(page_index)
        element = page.element(element_id) if page else None
        if element is None or page is None:
            return

        before_rect = element.rect.model_copy()
        before_text = element.text
        before_size = element.typography.size_pt if element.typography else None
        new_rect = Rect(
            x=self.x_spin.value(), y=self.y_spin.value(),
            width=self.w_spin.value(), height=self.h_spin.value(),
        )
        if not page.page_rect.contains(new_rect, tolerance=0.5):
            show_error(self, "Move frame", "The frame would fall outside the page.")
            return
        for other in page.elements:
            if other.id != element.id and other.type.value != "rule" and new_rect.overlaps(other.rect, 0.6):
                show_error(self, "Move frame", f"The frame would overlap '{other.frame_name}'.")
                return

        engine = self._engine
        new_text = self.text_edit.toPlainText()
        new_size = self.size_spin.value()

        def do() -> None:
            element.rect = new_rect
            element.text = new_text
            if element.typography is not None:
                ratio = element.typography.leading_pt / max(1e-6, element.typography.size_pt)
                element.typography.size_pt = new_size
                element.typography.leading_pt = round(new_size * ratio, 2)
            engine.refit(page)
            engine.rescore(page)

        def undo() -> None:
            element.rect = before_rect
            element.text = before_text
            if element.typography is not None and before_size is not None:
                ratio = element.typography.leading_pt / max(1e-6, element.typography.size_pt)
                element.typography.size_pt = before_size
                element.typography.leading_pt = round(before_size * ratio, 2)
            engine.refit(page)
            engine.rescore(page)

        self.app.undo.push(Command(label=f"Edit {element.frame_name}", do=do, undo=undo))
        self._fill_tree()
        self._render(page_index)
        self.status.setText(f"Page {page_index} re-scored: {page.score:.1f}")

    def _delete(self) -> None:
        selection = self._selected()
        if selection is None or self.plan is None:
            return
        page_index, element_id = selection
        page = self.plan.page(page_index)
        element = page.element(element_id) if page else None
        if element is None or page is None or element.locked:
            return
        position = page.elements.index(element)
        engine = self._engine

        def do() -> None:
            page.elements.remove(element)
            if engine:
                engine.rescore(page)

        def undo() -> None:
            page.elements.insert(position, element)
            if engine:
                engine.rescore(page)

        self.app.undo.push(Command(label=f"Delete {element.frame_name}", do=do, undo=undo))
        self._fill_tree()
        self._render(page_index)

    def _undo(self) -> None:
        label = self.app.undo.undo()
        if label:
            self._fill_tree()
            self.status.setText(f"Undone: {label}")

    def _redo(self) -> None:
        label = self.app.undo.redo()
        if label:
            self._fill_tree()
            self.status.setText(f"Redone: {label}")

    def _update_undo_buttons(self) -> None:
        self.undo_button.setEnabled(self.app.undo.can_undo)
        self.redo_button.setEnabled(self.app.undo.can_redo)
        self.undo_button.setToolTip(self.app.undo.undo_label() or "")
        self.redo_button.setToolTip(self.app.undo.redo_label() or "")

    # ------------------------------------------------------------- actions
    def _render(self, page_index: int | None = None) -> None:
        if self.plan is None or self._renderer is None or self.handle is None:
            return
        selection = self._selected()
        index = page_index if isinstance(page_index, int) else (selection[0] if selection else 1)
        page = self.plan.page(index)
        if page is None:
            return

        def action() -> None:
            target = self.handle.previews_dir / f"layout_page_{index:03d}.png"
            self._renderer.render_page(page, target)
            self.canvas.load(target)

        run_guarded(self, "Render page", action)

    def _rescore(self) -> None:
        selection = self._selected()
        if selection is None or self.plan is None or self._engine is None:
            return
        page = self.plan.page(selection[0])
        if page is None:
            return
        score, report = self._engine.rescore(page)
        self._fill_tree()
        issues = "; ".join(v.message for v in report.violations[:3])
        self.status.setText(
            f"Page {page.index}: score {score.total:.1f} - {len(report.violations)} issue(s)"
            + (f" - {issues}" if issues else "")
        )

    def _save_plan(self) -> None:
        if self.plan is None or self.handle is None:
            return

        def action() -> None:
            path = self.plan.save(self.handle.layout_plan_path)
            self.status.setText(f"Plan saved to {Path(path).name}")

        run_guarded(self, "Save plan", action)
