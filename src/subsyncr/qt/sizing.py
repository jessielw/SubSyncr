"""Window and dialog geometry, and the spacing rhythm.

These were scattered literals with no relationship to each other - 640x240,
660x520, 940x560, 1080x640. Collected here so a dialog that grows a tab is
resized somewhere its neighbours can be seen.
"""

from __future__ import annotations

# Default sizes, as (width, height).
SIZE_MAIN_WINDOW = (1240, 760)
SIZE_MAIN_MINIMUM = (960, 600)
SIZE_ADD_JOB = (660, 300)
SIZE_SETTINGS = (720, 560)
SIZE_HISTORY = (960, 560)
SIZE_MATCH = (1080, 640)

# One step of the spacing scale, in pixels. Fusion's own default is 6, which
# reads as cramped once a dialog carries more than a couple of rows.
SPACING = 8

#: Toolbar and menu icons.
ICON_SIZE = 18
#: Icons that sit inside a row of controls, next to a line edit.
INLINE_ICON_SIZE = 16

#: The engine fallback list is a picker, not a document; it should not grow.
FALLBACK_LIST_HEIGHT = 110
