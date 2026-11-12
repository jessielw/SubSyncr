"""Where file dialogs open: the folder last dropped on the window or browsed to.

Every browse button used to open wherever its own field pointed, and an empty
field meant the process's working directory - rarely anywhere near the media.
This keeps one shared answer, so picking a video in one dialog puts the next
dialog beside it.

The value lives in QSettings beside the window geometry: it is UI state, not a
setting anyone edits. Until ``attach`` is called it is kept in memory only, so
widgets built without a main window (and tests) never touch the user's real
settings.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

_KEY = "dialogs/lastDirectory"

_settings: QSettings | None = None
_remembered = ""


def attach(settings: QSettings) -> None:
    """Persist through these settings from now on."""
    global _settings, _remembered
    _settings = settings
    stored = settings.value(_KEY, "", str)
    _remembered = str(stored or "")


def detach() -> None:
    global _settings
    _settings = None


def remember(path: Path | str) -> None:
    """Record a file's folder, or a folder itself, as the place to open next."""
    global _remembered
    if not str(path).strip():
        return
    target = Path(path)
    folder = target if target.is_dir() else target.parent
    if not folder.is_dir():
        return
    _remembered = str(folder)
    if _settings is not None:
        _settings.setValue(_KEY, _remembered)


def last_directory() -> str:
    """The remembered folder, if it still exists; otherwise empty."""
    return _remembered if _remembered and Path(_remembered).is_dir() else ""


def start_for(current: str = "") -> str:
    """Where a dialog should open, given what its field already holds.

    A field that names something real wins - it is the most specific hint
    there is. An empty or stale one falls back to the remembered folder.
    """
    text = current.strip()
    if text:
        candidate = Path(text)
        if candidate.exists():
            return text
        if candidate.parent.is_dir() and str(candidate.parent) not in ("", "."):
            return str(candidate.parent)
    return last_directory()
