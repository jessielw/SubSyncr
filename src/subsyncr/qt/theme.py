"""Dark mode: Qt's own OS-tracking color scheme, with a user override.

Qt 6.5+ already watches the OS light/dark setting and repaints the Fusion
style live when it changes. All ``apply_theme`` does is let the user pin that
choice instead of following the OS - the same "Automatic / Light / Dark"
pattern NfoForge uses.

What Qt cannot repaint on its own is anything whose colour was baked in rather
than read from the palette: qtawesome renders each icon at a fixed colour, and
model/view code that hands back a ``QBrush`` keeps whatever it was given. So
``ThemeManager`` holds a registry of icon owners to re-tint, and emits
``changed`` for everything else that caches a colour.

Deliberately no stylesheet, anywhere. Under Fusion a QSS on a container makes
its children resolve colours from the stylesheet defaults rather than the
palette, which inverts enabled and disabled text - the reason the stylesheet
this application used to set was commented out before it was deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
import weakref

from PySide6.QtCore import QObject, QSize, Qt, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication
import qtawesome as qta
from shiboken6 import isValid

from subsyncr.core.config import Theme
from subsyncr.qt.icons import Icon

THEME_LABELS: dict[Theme, str] = {
    Theme.AUTOMATIC: "Automatic (follow system)",
    Theme.LIGHT: "Light",
    Theme.DARK: "Dark",
}

_COLOR_SCHEMES: dict[Theme, Qt.ColorScheme] = {
    Theme.LIGHT: Qt.ColorScheme.Light,
    Theme.DARK: Qt.ColorScheme.Dark,
}

# Anything that owns a QIcon and can be told to swap it.
IconTarget = QAction | QAbstractButton

DEFAULT_ICON_SIZE = 18


@dataclass(slots=True)
class _IconRegistration:
    target: weakref.ReferenceType[IconTarget]
    icon: Icon
    size: QSize | None


class ThemeManager(QObject):
    """Keeps baked-in colours in step with the active colour scheme.

    Parented to the QApplication, so it lives exactly as long as the app does
    and can be found again with ``findChild`` rather than a module global.
    """

    #: Emitted after every re-tint, for consumers that cache their own colours.
    changed = Signal()

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.app = app
        self._icons: list[_IconRegistration] = []
        app.styleHints().colorSchemeChanged.connect(self.refresh)

    @classmethod
    def for_app(cls, app: QApplication | None = None) -> ThemeManager | None:
        """The app's manager, created on first use.

        Widgets are built directly in tests that never call ``run_gui``, so
        this has to work whether or not the entry point already made one.
        """
        instance = app or QApplication.instance()
        if not isinstance(instance, QApplication):
            return None
        return instance.findChild(cls) or cls(instance)

    def icon_color(self) -> str:
        """The colour an icon has to be to read against the current window."""
        return self.app.palette().color(QPalette.ColorRole.WindowText).name()

    def icon(self, icon: Icon, color: str | None = None) -> QIcon:
        """A one-off icon at the current colour, for things too short-lived
        or too numerous to register - table cells, menu entries rebuilt on
        every show."""
        return qta.icon(str(icon), color=color or self.icon_color())

    def register(
        self, target: IconTarget, icon: Icon, size: int | None = DEFAULT_ICON_SIZE
    ) -> None:
        """Give ``target`` an icon now, and a fresh one on every theme change."""
        pixels = QSize(size, size) if size else None
        self._icons.append(_IconRegistration(weakref.ref(target), icon, pixels))
        self._apply(target, icon, pixels)

    @Slot()
    def refresh(self, *_args: object) -> None:
        """Re-tint every live registration and drop the dead ones.

        Takes ``*_args`` because ``colorSchemeChanged`` passes a colour scheme
        while a direct call passes nothing.
        """
        color = self.icon_color()
        live: list[_IconRegistration] = []
        for registration in self._icons:
            target = registration.target()
            # A PySide wrapper can outlive its C++ object, so a live weakref
            # is not on its own proof that setIcon is safe to call.
            if target is None or not isValid(target):
                continue
            self._apply(target, registration.icon, registration.size, color)
            live.append(registration)
        self._icons = live
        self.changed.emit()

    def _apply(
        self,
        target: IconTarget,
        icon: Icon,
        size: QSize | None,
        color: str | None = None,
    ) -> None:
        target.setIcon(self.icon(icon, color))
        # A QAction has no icon size of its own; whichever toolbar or menu
        # holds it decides.
        if size is not None and not isinstance(target, QAction):
            target.setIconSize(size)


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
    # Pinning Light while the OS is already Light changes nothing Qt will
    # report, so the refresh cannot be left to colorSchemeChanged.
    manager = app.findChild(ThemeManager)
    if manager is not None:
        manager.refresh()
