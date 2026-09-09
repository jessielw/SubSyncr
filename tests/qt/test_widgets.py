"""The shared widgets, and the file filters derived from the scanner."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QTableWidget
import pytest

from subsyncr.core.scanner import (
    MEDIA_EXTENSIONS,
    SPEECH_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
)
from subsyncr.qt.palette import alternate_base, warn_color
from subsyncr.qt.theme import ThemeManager
from subsyncr.qt.widgets import (
    MEDIA_FILTER,
    SUBTITLE_FILTER,
    FFmpegStatusLabel,
    PathField,
    enable_row_stripes,
)


@pytest.mark.parametrize("suffix", sorted(MEDIA_EXTENSIONS))
def test_media_filter_offers_everything_the_scanner_accepts(suffix: str) -> None:
    """These were hand-written and had drifted: browsing could not reach a
    .wmv that dropping onto the window accepted."""
    assert f"*{suffix}" in MEDIA_FILTER


@pytest.mark.parametrize("suffix", sorted(SUBTITLE_EXTENSIONS))
def test_subtitle_filter_offers_everything_the_scanner_accepts(
    suffix: str,
) -> None:
    assert f"*{suffix}" in SUBTITLE_FILTER


def test_media_filter_still_offers_serialized_speech() -> None:
    for suffix in SPEECH_EXTENSIONS:
        assert f"*{suffix}" in MEDIA_FILTER


def test_filters_end_with_an_escape_hatch() -> None:
    for value in (MEDIA_FILTER, SUBTITLE_FILTER):
        assert value.split(";;")[-1] == "All files (*)"


def test_path_field_round_trips_text(qtbot) -> None:
    field = PathField(caption="Select folder")
    qtbot.addWidget(field)
    field.set_text("  D:\\media\\Season01  ")
    assert field.text() == "D:\\media\\Season01"


def test_path_field_reports_changes(qtbot) -> None:
    seen: list[str] = []
    field = PathField(on_change=lambda: seen.append(field.text()))
    qtbot.addWidget(field)
    field.set_text("D:\\media")
    assert seen == ["D:\\media"]


def test_path_field_browse_button_has_an_icon(qtbot) -> None:
    field = PathField()
    qtbot.addWidget(field)
    assert not field.button.icon().isNull()


def test_ffmpeg_label_reports_a_missing_directory(qtbot, tmp_path: Path) -> None:
    label = FFmpegStatusLabel()
    qtbot.addWidget(label)
    assert label.refresh(str(tmp_path / "nowhere")) is False
    assert "does not exist" in label.text()


def test_ffmpeg_label_never_sets_a_stylesheet(qtbot, tmp_path: Path) -> None:
    """Colour comes from the palette. Under Fusion a stylesheet makes a
    widget's children resolve colours from the stylesheet defaults instead,
    which inverts enabled and disabled text."""
    label = FFmpegStatusLabel()
    qtbot.addWidget(label)
    label.refresh(str(tmp_path))
    assert not label.styleSheet()


def test_row_stripes_do_not_use_a_stylesheet(qtbot) -> None:
    table = QTableWidget(2, 2)
    qtbot.addWidget(table)
    enable_row_stripes(table)
    assert table.alternatingRowColors() is True
    assert not table.styleSheet()


def test_the_application_sets_no_stylesheet(qtbot) -> None:
    del qtbot
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    assert not application.styleSheet()


def test_ffmpeg_label_colour_tracks_the_scheme_immediately(
    qtbot, tmp_path: Path
) -> None:
    """The widget's own palette is still the old one while a theme change is
    being applied, so reading it here would leave the colour a change behind.
    """
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    manager = ThemeManager.for_app()
    assert manager is not None

    label = FFmpegStatusLabel()
    qtbot.addWidget(label)
    original = application.palette()
    try:
        label.refresh(str(tmp_path / "nowhere"))  # not ready -> warn colour

        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#2d2d2d"))
        application.setPalette(dark)
        manager.refresh()

        assert label.palette().color(label.foregroundRole()) == warn_color()
    finally:
        application.setPalette(original)


def test_row_stripes_track_the_scheme_immediately(qtbot) -> None:
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    manager = ThemeManager.for_app()
    assert manager is not None

    table = QTableWidget(2, 2)
    qtbot.addWidget(table)
    enable_row_stripes(table)
    original = application.palette()
    try:
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#2d2d2d"))
        application.setPalette(dark)
        manager.refresh()

        stripe = table.palette().color(QPalette.ColorRole.AlternateBase)
        assert stripe == alternate_base()
        assert stripe.lightness() > QColor("#2d2d2d").lightness()
    finally:
        application.setPalette(original)
