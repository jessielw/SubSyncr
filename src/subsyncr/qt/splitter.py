"""A splitter whose handle you can actually see.

Fusion paints a ``QSplitter`` handle as empty space, so the only sign that
two panes can be resized is the cursor changing somewhere along an invisible
seam. The handle here carries the same row of grip dots as the other Qt
applications in this family, drawn from the live palette so it follows a
change of colour scheme without being told about one.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from subsyncr.qt.sizing import SPLITTER_HANDLE

#: How many grip dots, how wide each one is, and the gap between centres.
_DOTS = 5
_DOT_SIZE = 2
_DOT_SPACING = 6


class GripSplitterHandle(QSplitterHandle):
    """A splitter handle marked with a row of grip dots."""

    def paintEvent(self, event: QPaintEvent) -> None:
        # Begin and end explicitly rather than letting the painter fall out of
        # scope: a handle can be repainted while the splitter it belongs to is
        # being resized or re-parented, and an unfinished painter reaching the
        # backing store warns once per frame for the length of the drag.
        painter = QPainter()
        if not painter.begin(self):
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Mid sits between Window and Shadow in every scheme, so the dots
            # read against the window without competing with the panes.
            color = self.palette().color(QPalette.ColorRole.Mid)
            painter.setPen(QPen(color, 1))
            painter.setBrush(color)

            center_x = self.width() // 2
            center_y = self.height() // 2
            offsets = range(-(_DOTS // 2), _DOTS // 2 + 1)
            # A vertical splitter stacks its panes, which makes its handle a
            # horizontal bar - so the dots run along the handle, not across it.
            if self.orientation() is Qt.Orientation.Vertical:
                for step in offsets:
                    _dot(painter, center_x + step * _DOT_SPACING, center_y)
            else:
                for step in offsets:
                    _dot(painter, center_x, center_y + step * _DOT_SPACING)
        finally:
            painter.end()


def _dot(painter: QPainter, x: int, y: int) -> None:
    offset = _DOT_SIZE // 2
    painter.drawEllipse(x - offset, y - offset, _DOT_SIZE, _DOT_SIZE)


class GripSplitter(QSplitter):
    """A ``QSplitter`` that draws a grip on each of its handles."""

    def __init__(
        self, orientation: Qt.Orientation, parent: QWidget | None = None
    ) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(SPLITTER_HANDLE)
        # A pane dragged shut reads as a bug rather than as a choice, and the
        # grip is the one thing that would have said otherwise.
        self.setChildrenCollapsible(False)

    def createHandle(self) -> QSplitterHandle:
        return GripSplitterHandle(self.orientation(), self)
