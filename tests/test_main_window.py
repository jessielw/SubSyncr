from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from ffsubsyncui.main_window import MainWindow
from ffsubsyncui.persistence import QueueStore


def test_main_window_starts_offscreen(qtbot: object, tmp_path: Path) -> None:
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(
        settings=settings, queue_store=QueueStore(tmp_path / "queue.json")
    )
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.show()
    assert window.windowTitle() == "FFSubSync UI"
    assert window.model.rowCount() == 0
