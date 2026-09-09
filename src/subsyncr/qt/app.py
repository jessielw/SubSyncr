"""QApplication construction and the GUI entry point."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from subsyncr import __version__

# STYLESHEET = """
# QToolBar {
#     border: 0; border-bottom: 1px solid palette(mid); padding: 6px; spacing: 4px;
# }
# QToolButton { padding: 6px 10px; border-radius: 5px; }
# QToolButton:hover { background: palette(midlight); }
# QToolButton:disabled { color: palette(mid); }
# QTableView {
#     border: 0; gridline-color: palette(midlight); selection-background-color: #2563eb;
# }
# QHeaderView::section {
#     padding: 7px; border: 0; border-bottom: 1px solid palette(mid); font-weight: 600;
# }
# QPushButton { padding: 6px 12px; }
# QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding: 5px; }
# QPlainTextEdit {
#     border: 0; border-top: 1px solid palette(mid); font-family: Consolas, monospace;
# }
# """


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    argv = list(arguments) if arguments is not None else sys.argv
    QCoreApplication.setOrganizationName("SubSyncr")
    QCoreApplication.setApplicationName("SubSyncr")
    QCoreApplication.setApplicationVersion(__version__)
    application = QApplication(argv)
    application.setStyle("Fusion")
    # application.setStyleSheet(STYLESHEET)
    return application


def run_gui(arguments: Sequence[str] | None = None) -> int:
    from subsyncr.core.config import AppConfig
    from subsyncr.qt.main_window import MainWindow
    from subsyncr.qt.theme import apply_theme

    # Loaded once here rather than inside MainWindow, so the saved theme is
    # in effect before any widget is created.
    config = AppConfig.load()
    application = create_application(list(arguments or []))
    apply_theme(config.theme, application)
    window = MainWindow(config=config)
    window.show()
    return application.exec()


def main(arguments: Sequence[str] | None = None) -> int:
    return run_gui(arguments)
