"""Application entry point.

Starts the Qt application, builds the object graph and shows the main window.
Any failure before the window exists is reported in a message box rather than
a traceback in a console the operator cannot see.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app import __app_name__, __version__

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(prog="ai-newspaper-studio", description=__app_name__)
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    parser.add_argument("--data-dir", type=Path, help="Override the user data directory")
    parser.add_argument("--project", help="Open this project on start (slug or folder)")
    parser.add_argument("--generate", action="store_true", help="Run the pipeline head-less and exit")
    parser.add_argument("--mode", default=None, choices=["auto", "semi_auto", "manual"])
    parser.add_argument("--diagnostics", action="store_true", help="Print diagnostics and exit")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def run_headless(args: argparse.Namespace) -> int:
    """Run the pipeline or the diagnostics without a window."""
    from app.application import create_application

    application = create_application(args.data_dir)
    try:
        if args.log_level:
            application.settings.set_path("log_level", args.log_level)
        if args.diagnostics:
            report = application.diagnostics.run(deep=True)
            print(report.render())
            return 0 if report.ok else 1
        if not args.project:
            print("--generate needs --project", file=sys.stderr)
            return 2
        handle = application.open_project(args.project)
        result = application.pipeline.run(handle, mode=args.mode)
        print(result.summary())
        for warning in result.warnings:
            print(f"  warning: {warning}")
        for path in result.pdf_paths:
            print(f"  pdf: {path}")
        return 0 if result.success else 1
    finally:
        application.shutdown()


def main(argv: list[str] | None = None) -> int:
    """Start the desktop application."""
    args = parse_args(argv)
    if args.diagnostics or args.generate:
        return run_headless(args)

    from PySide6.QtWidgets import QApplication, QMessageBox

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName(__app_name__)
    qt_app.setApplicationVersion(__version__)
    qt_app.setOrganizationName("AI Newspaper Studio")

    try:
        from app.application import create_application
        from app.ui.main_window import MainWindow
        from app.ui.theme import apply_theme

        application = create_application(args.data_dir)
        if args.log_level:
            application.settings.set_path("log_level", args.log_level)
        apply_theme(
            qt_app,
            application.settings.settings.ui.theme,
            application.settings.settings.ui.language,
        )
        window = MainWindow(application)
        if args.project:
            try:
                window.open_project(args.project)
            except Exception as exc:  # noqa: BLE001
                log.error("Could not open '%s': %s", args.project, exc)
        window.show()
    except Exception as exc:  # noqa: BLE001 - report instead of dying silently
        logging.exception("Start-up failed")
        QMessageBox.critical(
            None,
            f"{__app_name__} could not start",
            f"{type(exc).__name__}: {exc}\n\nSee the log for details.",
        )
        return 1

    exit_code = qt_app.exec()
    application.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
