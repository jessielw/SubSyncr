"""The theme manager, and the colours that have to follow it.

The offscreen QPA platform does not report colour-scheme changes, so these
drive the palette directly rather than going through ``styleHints()``.
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import QApplication, QToolButton
import pytest
from shiboken6 import delete

from subsyncr.core.models import JobStatus
from subsyncr.qt.icons import STATUS_ICONS, Icon
from subsyncr.qt.palette import (
    alternate_base,
    ambiguous_color,
    is_dark,
    ok_color,
    status_color,
    unmatched_color,
    warn_color,
)
from subsyncr.qt.theme import ThemeManager


def _palette(window: str, base: str, text: str) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(window))
    palette.setColor(QPalette.ColorRole.Base, QColor(base))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(text))
    return palette


# The values Qt's own Fusion light and dark schemes produce on Windows.
LIGHT = _palette("#f0f0f0", "#ffffff", "#000000")
DARK = _palette("#1e1e1e", "#2d2d2d", "#ffffff")


@pytest.fixture
def manager(qtbot) -> ThemeManager:
    del qtbot  # only needed so a QApplication exists
    instance = ThemeManager.for_app()
    assert instance is not None
    return instance


def test_for_app_returns_the_same_manager(manager: ThemeManager) -> None:
    """It is found again rather than duplicated, without a module global."""
    assert ThemeManager.for_app() is manager


def test_register_gives_a_button_a_real_icon(manager: ThemeManager, qtbot) -> None:
    button = QToolButton()
    qtbot.addWidget(button)
    manager.register(button, Icon.SETTINGS)
    assert not button.icon().isNull()
    assert not button.icon().pixmap(18, 18).isNull()


def test_register_gives_an_action_an_icon(manager: ThemeManager, qtbot) -> None:
    """QAction is the case NfoForge's swapper could not take, and every
    toolbar entry here is one."""
    button = QToolButton()
    qtbot.addWidget(button)
    action = QAction("Settings", button)
    manager.register(action, Icon.SETTINGS)
    assert not action.icon().isNull()


def test_refresh_retints_to_the_current_palette(manager: ThemeManager, qtbot) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    original = application.palette()
    button = QToolButton()
    qtbot.addWidget(button)
    try:
        application.setPalette(LIGHT)
        manager.register(button, Icon.SETTINGS)
        assert manager.icon_color() == "#000000"
        light_pixels = button.icon().pixmap(18, 18).toImage()

        application.setPalette(DARK)
        manager.refresh()
        assert manager.icon_color() == "#ffffff"
        assert button.icon().pixmap(18, 18).toImage() != light_pixels
    finally:
        application.setPalette(original)


def test_refresh_drops_a_deleted_target(manager: ThemeManager) -> None:
    """A PySide wrapper outlives its C++ object, so a live weakref is not on
    its own proof that setIcon is safe."""
    # The manager is app-scoped, so purge whatever earlier tests left behind
    # before counting.
    manager.refresh()
    baseline = len(manager._icons)

    button = QToolButton()
    manager.register(button, Icon.SETTINGS)
    assert len(manager._icons) == baseline + 1

    delete(button)  # destroys the C++ side, leaving the Python wrapper
    manager.refresh()  # must not raise

    assert len(manager._icons) == baseline


def test_refresh_emits_changed(manager: ThemeManager, qtbot) -> None:
    with qtbot.waitSignal(manager.changed, timeout=1000):
        manager.refresh()


def test_every_status_has_an_icon() -> None:
    assert set(STATUS_ICONS) == set(JobStatus)


def test_icon_names_are_all_phosphor() -> None:
    assert all(icon.value.startswith("ph.") for icon in Icon)


def test_is_dark_reads_the_window_colour() -> None:
    assert is_dark(DARK) is True
    assert is_dark(LIGHT) is False


@pytest.mark.parametrize("status", list(JobStatus))
def test_status_colours_differ_between_schemes(status: JobStatus) -> None:
    light = status_color(status, LIGHT)
    dark = status_color(status, DARK)
    assert light is not None and dark is not None
    # The dark variant has to be the lighter of the two to read on a dark row.
    assert dark.lightness() > light.lightness()


@pytest.mark.parametrize(
    "lookup", [ok_color, warn_color, unmatched_color, ambiguous_color]
)
def test_semantic_colours_lighten_on_dark(lookup) -> None:
    assert lookup(DARK).lightness() > lookup(LIGHT).lightness()


def test_alternate_base_stays_close_to_base() -> None:
    """Qt's dark scheme derives AlternateBase from the accent colour, which
    lands on a navy that reads as a selected row rather than a stripe."""
    for palette in (LIGHT, DARK):
        base = palette.color(QPalette.ColorRole.Base)
        stripe = alternate_base(palette)
        assert abs(stripe.lightness() - base.lightness()) < 40
        # A stripe with no hue of its own cannot be mistaken for a selection.
        assert stripe.saturation() < 20
