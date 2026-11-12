"""File dialogs open where the user last was."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog
import pytest

from subsyncr.qt import recent
from subsyncr.qt.widgets import MEDIA_FILTER, PathField


@pytest.fixture(autouse=True)
def _fresh(tmp_path: Path) -> Iterator[None]:
    """Start every test with nothing remembered and no real settings."""
    # A new file per test, so attach() starts from an empty value.
    recent.attach(QSettings(str(tmp_path / "recent.ini"), QSettings.Format.IniFormat))
    yield
    recent.detach()


def _season(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "Show" / "Season 01"
    folder.mkdir(parents=True)
    video = folder / "Show.S01E01.mkv"
    video.touch()
    return folder, video


def test_a_file_remembers_its_folder(tmp_path: Path) -> None:
    folder, video = _season(tmp_path)
    recent.remember(video)
    assert recent.last_directory() == str(folder)


def test_a_folder_remembers_itself(tmp_path: Path) -> None:
    folder, _ = _season(tmp_path)
    recent.remember(folder)
    assert recent.last_directory() == str(folder)


def test_a_vanished_folder_is_forgotten(tmp_path: Path) -> None:
    folder, video = _season(tmp_path)
    recent.remember(video)
    video.unlink()
    folder.rmdir()
    assert recent.last_directory() == ""


def test_it_survives_a_restart(tmp_path: Path) -> None:
    folder, video = _season(tmp_path)
    recent.remember(video)
    recent.attach(QSettings(str(tmp_path / "recent.ini"), QSettings.Format.IniFormat))
    assert recent.last_directory() == str(folder)


def test_a_filled_field_beats_the_remembered_folder(tmp_path: Path) -> None:
    folder, video = _season(tmp_path)
    recent.remember(tmp_path)
    assert recent.start_for(str(video)) == str(video)
    assert recent.start_for(str(folder / "gone.srt")) == str(folder)
    assert recent.start_for("") == str(tmp_path)


def _capture_dialog(monkeypatch: pytest.MonkeyPatch, answer: str) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake(_parent: object, _caption: str, start: str, *_: object) -> Any:
        seen["start"] = start
        return answer, ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", fake)
    return seen


def test_browsing_opens_at_and_updates_the_remembered_folder(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, video = _season(tmp_path)
    recent.remember(tmp_path)
    seen = _capture_dialog(monkeypatch, str(video))
    field = PathField(file_filter=MEDIA_FILTER)
    qtbot.addWidget(field)
    field.button.click()
    assert seen["start"] == str(tmp_path)
    assert field.text() == str(video)
    assert recent.last_directory() == str(folder)


def test_a_tool_field_neither_reads_nor_writes_it(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, video = _season(tmp_path)
    recent.remember(video)
    tools = tmp_path / "ffmpeg"
    tools.mkdir()
    seen = _capture_dialog(monkeypatch, str(tools / "ffmpeg.exe"))
    field = PathField(file_filter=MEDIA_FILTER, remember=False)
    qtbot.addWidget(field)
    field.button.click()
    assert seen["start"] == ""
    assert recent.last_directory() == str(video.parent)
