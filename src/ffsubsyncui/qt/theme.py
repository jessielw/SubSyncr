"""Dark mode: Qt's own OS-tracking color scheme, with a user override.

Qt 6.5+ already watches the OS light/dark setting and repaints the Fusion
style live when it changes. All this module does is let the user pin that
choice instead of following the OS - the same "Automatic / Light / Dark"
pattern NfoForge uses.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ffsubsyncui.core.config import Theme

THEME_LABELS: dict[Theme, str] = {
    Theme.AUTOMATIC: "Automatic (follow system)",
    Theme.LIGHT: "Light",
    Theme.DARK: "Dark",
}

_COLOR_SCHEMES: dict[Theme, Qt.ColorScheme] = {
    Theme.LIGHT: Qt.ColorScheme.Light,
    Theme.DARK: Qt.ColorScheme.Dark,
}


def apply_theme(theme: Theme, application: QApplication | None = None) -> None:
    """Pin the app to a color scheme, or hand control back to the OS."""
    app = application or QApplication.instance()
    if not isinstance(app, QApplication):
        return
    scheme = _COLOR_SCHEMES.get(theme)
    if scheme is not None:
        app.styleHints().setColorScheme(scheme)
    else:
        app.styleHints().unsetColorScheme()
