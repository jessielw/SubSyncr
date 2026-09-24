"""Widgets shared by more than one dialog.

Each of these existed two or three times over before it lived here: a line
edit with a browse button was written once per dialog, and the ffmpeg status
line was duplicated verbatim between the main window and the settings dialog.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, QSize, Slot
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from subsyncr.core import engines
from subsyncr.core.engines import EngineId
from subsyncr.core.ffmpeg import probe
from subsyncr.core.scanner import (
    MEDIA_EXTENSIONS,
    SPEECH_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    SUBTITLE_REFERENCE_EXTENSIONS,
)
from subsyncr.qt.icons import Icon
from subsyncr.qt.palette import alternate_base, ok_color, warn_color
from subsyncr.qt.sizing import INLINE_ICON_SIZE
from subsyncr.qt.theme import ThemeManager

ALL_FILES = "All files (*)"


def _patterns(suffixes: Iterable[str]) -> str:
    return " ".join(f"*{suffix}" for suffix in sorted(suffixes))


def _filter(*groups: tuple[str, Iterable[str]]) -> str:
    """Build a QFileDialog filter from the extension sets the scanner uses.

    Derived rather than written out so the browse dialog and the folder scan
    can never disagree about what counts as a media file - they did, and a
    .wmv that dropping onto the window accepted could not be browsed to.
    """
    parts = [f"{label} ({_patterns(suffixes)})" for label, suffixes in groups]
    parts.append(ALL_FILES)
    return ";;".join(parts)


MEDIA_FILTER = _filter(
    ("Media files", MEDIA_EXTENSIONS),
    ("Subtitle reference", SUBTITLE_REFERENCE_EXTENSIONS),
    ("Serialized speech", SPEECH_EXTENSIONS),
)
SUBTITLE_FILTER = _filter(("Subtitle files", SUBTITLE_EXTENSIONS))


class PathField(QWidget):
    """A line edit with a browse button, for a file or a directory."""

    def __init__(
        self,
        *,
        file_filter: str | None = None,
        caption: str = "",
        on_change: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # No filter means this field names a directory.
        self._filter = file_filter
        self._caption = caption or (
            "Select file" if file_filter is not None else "Select folder"
        )
        self.edit = QLineEdit(self)
        if on_change is not None:
            self.edit.textChanged.connect(lambda _: on_change())
        self.button = QToolButton(self)
        self.button.setToolTip("Browse...")
        self.button.clicked.connect(self._browse)
        manager = ThemeManager.for_app()
        if manager is not None:
            manager.register(self.button, Icon.BROWSE, INLINE_ICON_SIZE)
        else:  # pragma: no cover - only without a QApplication
            self.button.setText("...")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def text(self) -> str:
        return self.edit.text().strip()

    def set_text(self, value: str) -> None:
        self.edit.setText(value)

    @Slot()
    def _browse(self) -> None:
        if self._filter is None:
            selected = QFileDialog.getExistingDirectory(
                self, self._caption, self.text()
            )
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self, self._caption, self.text(), self._filter
            )
        if selected:
            self.set_text(selected)


class EngineCombo(QComboBox):
    """Every registered engine, for picking which one a batch runs with."""

    def __init__(self, engine: EngineId, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._default = engine
        for spec in engines.all_specs():
            self.addItem(spec.label, spec.id)
        self.set_engine(engine)

    def engine(self) -> EngineId:
        # Qt hands enum item data back as a plain str; parse it back.
        return EngineId.parse(self.currentData(), self._default)

    def set_engine(self, engine: EngineId) -> None:
        index = self.findData(engine)
        if index >= 0:
            self.setCurrentIndex(index)


class FFmpegStatusLabel(QLabel):
    """Whether ffmpeg can be found, in a colour that suits the theme."""

    def __init__(self, parent: QWidget | None = None, *, wrap: bool = True) -> None:
        super().__init__(parent)
        # A status bar has one line to give; a form row can take as many as
        # the message needs.
        self.setWordWrap(wrap)
        self._wrap = wrap
        self._ready = False
        manager = ThemeManager.for_app()
        if manager is not None:
            manager.changed.connect(self._recolor)

    def refresh(self, configured_path: str = "") -> bool:
        status = probe(configured_path)
        self._ready = status.ready
        self.setText(status.message)
        if not self._wrap:
            # Elided rather than truncated, so a long ffmpeg path still shows
            # the part that identifies it.
            self.setToolTip(status.message)
        self._recolor()
        return status.ready

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._wrap:
            return hint
        # Without this a long path forces the whole status bar wider.
        hint.setWidth(0)
        return hint

    @Slot()
    def _recolor(self) -> None:
        # From the application palette, not this widget's: during a theme
        # change the widget still holds the old one, because Qt propagates it
        # by posted event. A default QPalette has no resolve bits, so this
        # overrides the text colour and leaves the rest inherited.
        override = QPalette()
        override.setColor(
            self.foregroundRole(), ok_color() if self._ready else warn_color()
        )
        self.setPalette(override)


class _RowStripes(QObject):
    """Keeps a view's alternating row colour readable across theme changes.

    Parented to the view, so the connection to the theme manager dies with it
    rather than firing at a deleted C++ object.
    """

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        manager = ThemeManager.for_app()
        if manager is not None:
            manager.changed.connect(self.apply)
        self.apply()

    @Slot()
    def apply(self) -> None:
        # Computed from the application palette, which setColorScheme updates
        # synchronously; the widget's own palette is still the old one at this
        # point, because Qt propagates that to widgets by posted event.
        #
        # A default QPalette carries no resolve bits, so assigning one that
        # sets a single role overrides just that role and leaves everything
        # else inherited - and still following the scheme.
        override = QPalette()
        override.setColor(QPalette.ColorRole.AlternateBase, alternate_base())
        self._view.setPalette(override)


def enable_row_stripes(view: QAbstractItemView) -> None:
    """Turn on alternating rows, in a colour that suits the scheme."""
    view.setAlternatingRowColors(True)
    _RowStripes(view)
