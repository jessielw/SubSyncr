from __future__ import annotations

from collections.abc import Sequence
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from ffsubsyncui.main_window import MainWindow

STYLESHEET = """
QMainWindow { background: palette(window); }
QToolBar {
    border: 0; border-bottom: 1px solid palette(mid); padding: 6px; spacing: 4px;
}
QToolButton { padding: 6px 8px; border-radius: 5px; }
QToolButton:hover { background: palette(midlight); }
QTableView {
    border: 0; gridline-color: palette(midlight); selection-background-color: #2563eb;
}
QHeaderView::section {
    padding: 7px; border: 0; border-bottom: 1px solid palette(mid); font-weight: 600;
}
QPushButton { padding: 6px 12px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding: 5px; }
QPlainTextEdit {
    border: 0; border-top: 1px solid palette(mid); font-family: Consolas, monospace;
}
"""


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    argv = list(arguments) if arguments is not None else sys.argv
    QCoreApplication.setOrganizationName("FFSubSyncUI")
    QCoreApplication.setApplicationName("FFSubSync UI")
    QCoreApplication.setApplicationVersion("0.1.0")
    application = QApplication(argv)
    application.setStyle("Fusion")
    application.setStyleSheet(STYLESHEET)
    return application


def main(arguments: Sequence[str] | None = None) -> int:
    application = create_application(arguments)
    window = MainWindow()
    window.show()
    return application.exec()
