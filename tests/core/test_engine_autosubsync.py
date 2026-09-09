"""autosubsync: the parts that are ours rather than the library's."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from support import sync_options

from subsyncr.core.engines import autosubsync


def test_the_packaged_model_can_be_found() -> None:
    """Without this the library falls back to pkg_resources, which is gone."""
    model = autosubsync.model_path()
    assert model is not None, "autosubsync's trained-model.bin was not located"
    assert model.is_file()


def test_a_missing_model_fails_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(autosubsync, "model_path", lambda: None)
    outcome = autosubsync.align(
        "ep.mkv", tmp_path / "in.srt", tmp_path / "out.srt", sync_options().to_dict()
    )
    assert outcome.status == "failed"
    assert "trained model" in outcome.error


def test_a_configured_ffmpeg_folder_is_put_on_path(tmp_path: Path) -> None:
    """autosubsync runs `ffmpeg` by bare name; PATH is the only lever."""
    patched = autosubsync._path_with_ffmpeg(str(tmp_path))
    assert patched is not None
    assert patched.split(os.pathsep)[0] == str(tmp_path)


def test_an_ffmpeg_binary_contributes_its_folder(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg.exe"
    binary.touch()
    patched = autosubsync._path_with_ffmpeg(str(binary))
    assert patched is not None
    assert patched.split(os.pathsep)[0] == str(tmp_path)


def test_path_is_left_alone_when_nothing_is_configured() -> None:
    assert autosubsync._path_with_ffmpeg("") is None
    assert autosubsync._path_with_ffmpeg("  ") is None
    assert autosubsync._path_with_ffmpeg("/nowhere/at/all") is None


def _fake_synchronize(result: tuple[Any, ...]) -> Any:
    def synchronize(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        # It writes the shifted file whatever it thinks of the fit.
        Path(args[2]).write_text("shifted", encoding="utf-8")
        synchronize.seen = kwargs  # type: ignore[attr-defined]
        return result

    return synchronize


def test_a_poor_fit_is_reported_as_low_quality_not_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It writes the file regardless, so a bad fit must not reach the user."""
    import autosubsync.main as library

    monkeypatch.setattr(
        library, "synchronize", _fake_synchronize((False, 0.2, 1.0, -3.5))
    )
    outcome = autosubsync.align(
        "ep.mkv",
        tmp_path / "in.srt",
        tmp_path / "staged.srt",
        sync_options().to_dict(),
    )
    assert outcome.status == "skipped_low_quality"
    assert outcome.quality_reasons
    assert outcome.offset_seconds == -3.5
    assert outcome.score == 0.2


def test_a_good_fit_reports_its_shift_and_skew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import autosubsync.main as library

    monkeypatch.setattr(
        library, "synchronize", _fake_synchronize((True, 9.1, 1.042, 12.0))
    )
    outcome = autosubsync.align(
        "ep.mkv", tmp_path / "in.srt", tmp_path / "out.srt", sync_options().to_dict()
    )
    assert outcome.status == "succeeded"
    assert outcome.offset_seconds == 12.0
    assert outcome.framerate_scale_factor == 1.042
    assert outcome.score == 9.1


def test_a_skew_of_one_is_reported_as_no_correction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import autosubsync.main as library

    monkeypatch.setattr(
        library, "synchronize", _fake_synchronize((True, 9.1, 1.0, 4.0))
    )
    outcome = autosubsync.align(
        "ep.mkv", tmp_path / "in.srt", tmp_path / "out.srt", sync_options().to_dict()
    )
    assert outcome.framerate_scale_factor is None


def test_path_is_restored_even_when_the_library_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import autosubsync.main as library

    def explode(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        raise RuntimeError("ffmpeg went missing")

    monkeypatch.setattr(library, "synchronize", explode)
    options = sync_options()
    options.environment.ffmpeg_path = str(tmp_path)
    before = os.environ.get("PATH", "")
    with pytest.raises(RuntimeError):
        autosubsync.align(
            "ep.mkv", tmp_path / "in.srt", tmp_path / "out.srt", options.to_dict()
        )
    assert os.environ.get("PATH", "") == before
