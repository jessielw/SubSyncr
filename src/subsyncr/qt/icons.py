"""The icon vocabulary, and the application mark.

Icons are Phosphor glyphs rendered by qtawesome, which is what NfoForge and
FFmpeg Audio Encoder both use. Naming them here rather than sprinkling
``"ph.gear-light"`` through the widgets keeps the set reviewable, and means a
typo shows up once instead of at whichever call site happens to run first.

Nothing in this module renders anything at import time: ``qta.icon`` needs a
live QApplication, so every lookup goes through ``subsyncr.qt.theme``.
"""

from __future__ import annotations

from enum import StrEnum

from subsyncr.core.models import JobStatus


class Icon(StrEnum):
    """A qtawesome name, chosen for what it means rather than what it draws."""

    # Toolbar.
    MATCH = "ph.folders-light"
    ADD = "ph.plus-circle-light"
    REMOVE = "ph.trash-simple-light"
    RETRY = "ph.arrow-counter-clockwise-light"
    CLEAR = "ph.eraser-light"
    START = "ph.play-light"
    STOP = "ph.pause-light"
    CANCEL = "ph.stop-light"
    HISTORY = "ph.clock-counter-clockwise-light"
    SETTINGS = "ph.gear-light"

    # Dialogs.
    BROWSE = "ph.folder-open-light"
    SCAN = "ph.magnifying-glass-light"
    CHOOSE = "ph.file-search-light"
    ABOUT = "ph.info-light"
    QUIT = "ph.sign-out-light"
    OPEN_FOLDER = "ph.arrow-square-out-light"

    # Job status, for the queue table's decoration column.
    QUEUED = "ph.clock-light"
    RUNNING = "ph.arrows-clockwise-light"
    SUCCEEDED = "ph.check-circle-light"
    FAILED = "ph.x-circle-light"
    CANCELLED = "ph.prohibit-light"
    LOW_QUALITY = "ph.warning-light"


STATUS_ICONS: dict[JobStatus, Icon] = {
    JobStatus.QUEUED: Icon.QUEUED,
    JobStatus.RUNNING: Icon.RUNNING,
    JobStatus.SUCCEEDED: Icon.SUCCEEDED,
    JobStatus.FAILED: Icon.FAILED,
    JobStatus.CANCELLED: Icon.CANCELLED,
    JobStatus.SKIPPED_LOW_QUALITY: Icon.LOW_QUALITY,
}


# A video frame carrying two subtitle lines, and the playhead they are being
# lined up against. Kept to three shapes because the smallest size this is
# asked for is 16px.
#
# Embedded rather than shipped as a file: the application otherwise has no
# runtime data at all, and adding some would mean teaching the frozen build
# where to find it. ``currentColor`` is substituted at render time, so the
# same markup serves the window icon and the build-time .ico.
APP_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="4" y="10" width="56" height="44" rx="8"
        fill="none" stroke="currentColor" stroke-width="4"/>
  <rect x="14" y="34" width="26" height="5" rx="2.5" fill="currentColor"/>
  <rect x="14" y="43" width="16" height="5" rx="2.5" fill="currentColor"/>
  <path d="M52 36.5 L44 41.5 L44 31.5 Z" fill="currentColor"/>
</svg>
"""
