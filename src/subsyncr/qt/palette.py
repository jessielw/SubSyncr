"""Semantic colours that survive a change of colour scheme.

Qt repaints everything it owns when the scheme flips, but a colour this
application chose itself - a red for "failed", an amber for "look at this" -
is only ever as good as the background it was picked against. The values here
come in pairs: the darker shade reads on a light window, the lighter one on a
dark window, and the live palette decides which is in force.

Colours are looked up per paint rather than cached, so a repaint after a
scheme change is already correct. Anything that *must* cache - a QBrush baked
into a QTableWidgetItem - should listen to ``ThemeManager.changed``.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from subsyncr.core.models import JobStatus

# Tailwind 700 on light, 400 on dark: the same hue at the lightness each
# background needs. The 700 shades are what the application already used.
_STATUS_COLORS: dict[JobStatus, tuple[str, str]] = {
    JobStatus.QUEUED: ("#6b7280", "#9ca3af"),
    JobStatus.RUNNING: ("#2563eb", "#60a5fa"),
    JobStatus.SUCCEEDED: ("#15803d", "#4ade80"),
    JobStatus.FAILED: ("#b91c1c", "#f87171"),
    JobStatus.CANCELLED: ("#78716c", "#a8a29e"),
    JobStatus.SKIPPED_LOW_QUALITY: ("#b45309", "#fbbf24"),
}

_OK = ("#15803d", "#4ade80")
_WARN = ("#b45309", "#fbbf24")
# Rows the matcher could not pair at all, and rows whose runner-up was close.
_UNMATCHED = ("#b91c1c", "#f87171")
_AMBIGUOUS = ("#b45309", "#fbbf24")


def active_palette(palette: QPalette | None = None) -> QPalette:
    return palette if palette is not None else QApplication.palette()


def is_dark(palette: QPalette | None = None) -> bool:
    """Whether the window this will be painted on is a dark one.

    Read from the palette rather than ``styleHints().colorScheme()`` because
    the palette is what actually gets painted, and it is correct under
    "Automatic" without asking the OS a second question.
    """
    return active_palette(palette).color(QPalette.ColorRole.Window).lightness() < 128


def _pick(pair: tuple[str, str], palette: QPalette | None) -> QColor:
    light, dark = pair
    return QColor(dark if is_dark(palette) else light)


def status_color(status: JobStatus, palette: QPalette | None = None) -> QColor | None:
    pair = _STATUS_COLORS.get(status)
    return None if pair is None else _pick(pair, palette)


def alternate_base(palette: QPalette | None = None) -> QColor:
    """A row stripe that actually reads as a stripe.

    Qt's dark colour scheme derives AlternateBase from the system accent
    colour - on Windows that lands on a saturated navy (#001a68) which reads
    as a selected row rather than a stripe. Nudging Base is what the light
    scheme does anyway, so do it in both and the two match.
    """
    resolved = active_palette(palette)
    base = resolved.color(QPalette.ColorRole.Base)
    return base.lighter(118) if is_dark(resolved) else base.darker(104)


def ok_color(palette: QPalette | None = None) -> QColor:
    return _pick(_OK, palette)


def warn_color(palette: QPalette | None = None) -> QColor:
    return _pick(_WARN, palette)


def unmatched_color(palette: QPalette | None = None) -> QColor:
    return _pick(_UNMATCHED, palette)


def ambiguous_color(palette: QPalette | None = None) -> QColor:
    return _pick(_AMBIGUOUS, palette)
