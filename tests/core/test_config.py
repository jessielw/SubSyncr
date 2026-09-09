"""Configuration loading, clamping and atomic saving."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import sync_options

from subsyncr.core.config import (
    MAX_CONCURRENCY,
    AppConfig,
    Theme,
    default_concurrency,
)


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(
        sync=sync_options(vad="auditok", split_penalty=5.0),
        concurrency=3,
        output_suffix=".fixed",
    )
    config.save(path)
    restored = AppConfig.load(path)
    assert restored.concurrency == 3
    assert restored.output_suffix == ".fixed"
    assert restored.sync.ffsubsync.vad == "auditok"
    assert restored.sync.ffsubsync.split_penalty == 5.0


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    loaded = AppConfig.load(tmp_path / "absent.json")
    assert loaded.concurrency == default_concurrency()
    assert loaded.auto_start is True


def test_corrupt_file_yields_defaults(tmp_path: Path) -> None:
    """A broken config must not stop the application starting."""
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert AppConfig.load(path).concurrency == default_concurrency()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 1), (-5, 1), (MAX_CONCURRENCY + 10, MAX_CONCURRENCY), (3, 3)],
)
def test_concurrency_is_clamped(value: int, expected: int) -> None:
    assert AppConfig(concurrency=value).normalized().concurrency == expected


def test_default_concurrency_is_sane() -> None:
    """Derived from the machine, but never outside what the app accepts."""
    value = default_concurrency()
    assert 1 <= value <= MAX_CONCURRENCY


def test_auto_start_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    AppConfig(auto_start=False).save(path)
    assert AppConfig.load(path).auto_start is False


def test_theme_defaults_to_automatic() -> None:
    assert AppConfig().theme is Theme.AUTOMATIC


def test_theme_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    AppConfig(theme=Theme.DARK).save(path)
    assert AppConfig.load(path).theme is Theme.DARK


def test_unknown_theme_falls_back_to_automatic(tmp_path: Path) -> None:
    """A hand-edited or stale config value must not stop the app starting."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "solarized"}), encoding="utf-8")
    assert AppConfig.load(path).theme is Theme.AUTOMATIC


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
    from subsyncr.core import config as config_module

    monkeypatch.setenv("SUBSYNCR_DATA_DIR", str(tmp_path / "portable"))
    assert config_module.data_dir() == tmp_path / "portable"
    assert config_module.database_path().parent == tmp_path / "portable"
    assert config_module.speech_cache_dir().is_dir()
