"""Application theme.

A single stylesheet drives the whole UI so the pages stay visually
consistent, and the palette follows the operator's light/dark choice. The
layout direction follows the interface language, which matters for the
Persian and Arabic interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

RTL_LANGUAGES = {"fa", "ar"}


@dataclass(frozen=True)
class Palette:
    """The colours a theme is built from."""

    background: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    accent: str
    accent_text: str
    success: str
    warning: str
    danger: str

    @classmethod
    def dark(cls) -> Palette:
        """The dark theme."""
        return cls(
            background="#14161a", surface="#1c1f25", surface_alt="#23272f", border="#2f343d",
            text="#e8eaed", text_muted="#9aa2ae", accent="#3f8cff", accent_text="#ffffff",
            success="#3fbf7f", warning="#e0a33e", danger="#e0554a",
        )

    @classmethod
    def light(cls) -> Palette:
        """The light theme."""
        return cls(
            background="#f4f5f7", surface="#ffffff", surface_alt="#eceef1", border="#d6d9de",
            text="#1a1d21", text_muted="#5c636d", accent="#1a6fe0", accent_text="#ffffff",
            success="#1f9d5e", warning="#b57a12", danger="#c73a2e",
        )


def palette_for(theme: str) -> Palette:
    """Return the palette for ``"dark"``, ``"light"`` or ``"system"``."""
    if theme == "light":
        return Palette.light()
    if theme == "system":
        hints = QApplication.styleHints()
        scheme = getattr(hints, "colorScheme", None)
        if scheme is not None and scheme() == Qt.ColorScheme.Light:
            return Palette.light()
    return Palette.dark()


def stylesheet(palette: Palette) -> str:
    """Build the application stylesheet from *palette*."""
    return f"""
    QWidget {{
        background-color: {palette.background};
        color: {palette.text};
        font-size: 13px;
    }}
    QFrame#Card, QGroupBox {{
        background-color: {palette.surface};
        border: 1px solid {palette.border};
        border-radius: 8px;
    }}
    QGroupBox {{
        margin-top: 14px;
        padding: 14px 12px 12px 12px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: {palette.text_muted};
    }}
    QLabel#Title {{ font-size: 22px; font-weight: 600; }}
    QLabel#Subtitle {{ color: {palette.text_muted}; font-size: 13px; }}
    QLabel#Metric {{ font-size: 26px; font-weight: 700; }}
    QLabel#MetricLabel {{ color: {palette.text_muted}; font-size: 12px; }}

    QListWidget#Nav {{
        background-color: {palette.surface};
        border: none;
        border-right: 1px solid {palette.border};
        outline: none;
        padding: 8px 6px;
    }}
    QListWidget#Nav::item {{
        padding: 9px 12px;
        border-radius: 6px;
        margin: 2px 4px;
        color: {palette.text_muted};
    }}
    QListWidget#Nav::item:selected {{
        background-color: {palette.accent};
        color: {palette.accent_text};
    }}
    QListWidget#Nav::item:hover:!selected {{ background-color: {palette.surface_alt}; }}

    QPushButton {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 6px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{ border-color: {palette.accent}; }}
    QPushButton:disabled {{ color: {palette.text_muted}; border-color: {palette.border}; }}
    QPushButton#Primary {{
        background-color: {palette.accent};
        color: {palette.accent_text};
        border: none;
        font-weight: 600;
        padding: 9px 18px;
    }}
    QPushButton#Danger {{ background-color: {palette.danger}; color: #ffffff; border: none; }}

    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 6px;
        padding: 6px 8px;
        selection-background-color: {palette.accent};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {palette.accent}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}

    QTableWidget, QTreeWidget, QListWidget {{
        background-color: {palette.surface};
        border: 1px solid {palette.border};
        border-radius: 8px;
        gridline-color: {palette.border};
        alternate-background-color: {palette.surface_alt};
    }}
    QHeaderView::section {{
        background-color: {palette.surface_alt};
        border: none;
        border-bottom: 1px solid {palette.border};
        padding: 7px 8px;
        font-weight: 600;
    }}
    QTableWidget::item:selected, QListWidget::item:selected {{
        background-color: {palette.accent};
        color: {palette.accent_text};
    }}

    QProgressBar {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: 6px;
        height: 16px;
        text-align: center;
    }}
    QProgressBar::chunk {{ background-color: {palette.accent}; border-radius: 5px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {palette.border}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {palette.border}; border-radius: 5px; min-width: 30px; }}

    QTabWidget::pane {{ border: 1px solid {palette.border}; border-radius: 8px; }}
    QTabBar::tab {{
        background: {palette.surface_alt};
        border: 1px solid {palette.border};
        padding: 7px 14px;
        margin-right: 3px;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
    }}
    QTabBar::tab:selected {{ background: {palette.accent}; color: {palette.accent_text}; }}

    QStatusBar {{ background: {palette.surface}; border-top: 1px solid {palette.border}; }}
    QToolTip {{
        background-color: {palette.surface_alt};
        color: {palette.text};
        border: 1px solid {palette.border};
        padding: 4px;
    }}
    QSplitter::handle {{ background: {palette.border}; }}
    """


def apply_theme(application: QApplication, theme: str, language: str) -> Palette:
    """Apply the theme and reading direction to the whole application."""
    palette = palette_for(theme)
    application.setStyleSheet(stylesheet(palette))
    application.setLayoutDirection(
        Qt.LayoutDirection.RightToLeft if language in RTL_LANGUAGES else Qt.LayoutDirection.LeftToRight
    )

    qt_palette = QPalette()
    qt_palette.setColor(QPalette.ColorRole.Window, QColor(palette.background))
    qt_palette.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Base, QColor(palette.surface))
    qt_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Button, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
    qt_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(palette.accent_text))
    application.setPalette(qt_palette)

    application.setFont(ui_font(language))
    return palette


def ui_font(language: str) -> QFont:
    """Interface font, preferring a face that carries the script in use."""
    families = (
        ["Vazirmatn", "IRANSans", "Segoe UI", "Tahoma", "Noto Sans Arabic", "DejaVu Sans"]
        if language in RTL_LANGUAGES
        else ["Inter", "Segoe UI", "Helvetica Neue", "DejaVu Sans"]
    )
    font = QFont()
    font.setFamilies(families)
    font.setPointSize(10)
    return font


SEVERITY_COLORS = {
    "critical": "danger",
    "error": "danger",
    "high": "danger",
    "warning": "warning",
    "medium": "warning",
    "low": "text_muted",
    "info": "text_muted",
    "ok": "success",
}


def severity_color(palette: Palette, severity: str) -> str:
    """Colour for a severity or status word."""
    return getattr(palette, SEVERITY_COLORS.get(severity.lower(), "text_muted"))
