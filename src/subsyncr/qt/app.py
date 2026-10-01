"""QApplication construction and the GUI entry point."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from PySide6.QtCore import QByteArray, QCoreApplication, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMessageBox

from subsyncr import __version__
from subsyncr.qt.icons import APP_ICON_SVG

# The sizes a window icon is actually asked for: title bar, alt-tab, and the
# large form some desktops use in their task switcher.
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def application_icon(color: str = "#4f9de0") -> QIcon:
    """Render the embedded mark at the sizes a desktop asks for.

    Rendered rather than loaded so the application ships no data files, which
    keeps the frozen build free of any path resolution.
    """
    markup = APP_ICON_SVG.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(markup.encode("utf-8")))
    icon = QIcon()
    for size in _ICON_SIZES:
        image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(QPixmap.fromImage(image))
    return icon


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    argv = list(arguments) if arguments is not None else sys.argv
    QCoreApplication.setOrganizationName("SubSyncr")
    QCoreApplication.setApplicationName("SubSyncr")
    QCoreApplication.setApplicationVersion(__version__)
    application = QApplication(argv)
    # Fusion is what makes Qt's light/dark colour scheme render the same on
    # every platform. No stylesheet is set, deliberately: under Fusion a QSS
    # makes a container's children resolve colours from the stylesheet
    # defaults rather than the palette, which inverts disabled text.
    application.setStyle("Fusion")
    application.setWindowIcon(application_icon())
    # Names the .desktop entry a Linux desktop should match this window to.
    application.setDesktopFileName("subsyncr")
    return application


def run_gui(arguments: Sequence[str] | None = None) -> int:
    from subsyncr.core import logger
    from subsyncr.core.config import AppConfig, data_dir, log_dir
    from subsyncr.qt.errors import ErrorReporter
    from subsyncr.qt.instance import acquire_gui_lock
    from subsyncr.qt.main_window import MainWindow
    from subsyncr.qt.theme import ThemeManager, apply_theme

    application = create_application(list(arguments or []))
    ErrorReporter.install()
    # Acquire before reading the queue or opening the rotating log file. The
    # CLI uses a non-persisting queue and does not need this GUI ownership lock.
    try:
        lock = acquire_gui_lock(data_dir())
    except (OSError, RuntimeError) as exc:
        QMessageBox.warning(None, "SubSyncr", str(exc))
        return 1
    try:
        logger.configure(log_dir() / logger.LOG_FILE_NAME)
        config = AppConfig.load()
        logger.set_level(config.log_level)
        logger.set_retention(config.log_retention_days)
        ThemeManager.for_app(application)
        apply_theme(config.theme, application)
        window = MainWindow(config=config)
        window.show()
        return application.exec()
    finally:
        logger.shutdown()
        lock.unlock()


def main(arguments: Sequence[str] | None = None) -> int:
    return run_gui(arguments)
