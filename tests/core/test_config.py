"""Configuration loading, clamping and atomic saving."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffsubsyncui.core.config import MAX_CONCURRENCY, AppConfig
from ffsubsyncui.core.models import SyncOptions


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(
        sync=SyncOptions(vad="auditok", split_penalty=5.0),
        concurrency=3,
        output_suffix=".fixed",
    )
    config.save(path)
    restored = AppConfig.load(path)
    assert restored.concurrency == 3
    assert restored.output_suffix == ".fixed"
    assert restored.sync.vad == "auditok"
    assert restored.sync.split_penalty == 5.0


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    assert AppConfig.load(tmp_path / "absent.json").concurrency == 1


def test_corrupt_file_yields_defaults(tmp_path: Path) -> None:
    """A broken config must not stop the application starting."""
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert AppConfig.load(path).concurrency == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 1), (-5, 1), (MAX_CONCURRENCY + 10, MAX_CONCURRENCY), (3, 3)],
)
def test_concurrency_is_clamped(value: int, expected: int) -> None:
    assert AppConfig(concurrency=value).normalized().concurrency == expected


def test_threshold_is_clamped() -> None:
    assert AppConfig(match_threshold=500.0).normalized().match_threshold == 100.0
    assert AppConfig(match_threshold=-1.0).normalized().match_threshold == 0.0


def test_empty_output_suffix_falls_back(tmp_path: Path) -> None:
    """An empty suffix would make a job overwrite its own input."""
    assert AppConfig(output_suffix="   ").normalized().output_suffix == ".synced"


def test_suffix_gains_a_leading_dot() -> None:
    assert AppConfig(output_suffix="fixed").normalized().output_suffix == ".fixed"


def test_save_is_atomic(tmp_path: Path) -> None:
    """A crash mid-save must not leave a truncated config behind."""
    path = tmp_path / "config.json"
    AppConfig(concurrency=2).save(path)
    AppConfig(concurrency=4).save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["concurrency"] == 4
    assert not list(tmp_path.glob(".config-*.tmp"))


def test_data_dir_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Used by the tests and by anyone wanting a portable install."""
    from ffsubsyncui.core import config as config_module

    monkeypatch.setenv("FFSUBSYNCUI_DATA_DIR", str(tmp_path / "portable"))
    assert config_module.data_dir() == tmp_path / "portable"
    assert config_module.database_path().parent == tmp_path / "portable"
    assert config_module.speech_cache_dir().is_dir()
