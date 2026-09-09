"""Application configuration and the per-user paths it lives in.

Deliberately independent of Qt: the CLI reads the same settings the GUI writes.
Window geometry is *not* here - that is genuine UI state and stays in QSettings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Self

from platformdirs import user_cache_dir, user_data_dir

from ffsubsyncui.core.models import SyncOptions

APP_NAME = "FFSubSyncUI"
APP_AUTHOR = "FFSubSyncUI"


class Theme(StrEnum):
    """Automatic tracks the OS color scheme; Light/Dark pin it explicitly."""

    AUTOMATIC = "automatic"
    LIGHT = "light"
    DARK = "dark"


# ffsubsync is CPU and IO heavy per job; more than a handful of concurrent
# extractions just thrashes the disk.
MAX_CONCURRENCY = 8


def default_concurrency() -> int:
    """A concurrency that helps on a workstation without thrashing a disk.

    Speech extraction is mostly a serial read of the reference plus an ffmpeg
    audio decode, so it scales well up to a point. Half the logical cores,
    capped at four, keeps headroom for whatever else is encoding.
    """
    return min(4, max(1, (os.cpu_count() or 2) // 2))


_ENV_DATA_DIR = "FFSUBSYNCUI_DATA_DIR"


def data_dir() -> Path:
    override = os.environ.get(_ENV_DATA_DIR, "").strip()
    directory = (
        Path(override).expanduser()
        if override
        else Path(user_data_dir(APP_NAME, APP_AUTHOR, roaming=False))
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def cache_dir() -> Path:
    override = os.environ.get(_ENV_DATA_DIR, "").strip()
    directory = (
        Path(override).expanduser() / "cache"
        if override
        else Path(user_cache_dir(APP_NAME, APP_AUTHOR))
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def speech_cache_dir() -> Path:
    directory = cache_dir() / "speech"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_path() -> Path:
    return data_dir() / "config.json"


def database_path() -> Path:
    return data_dir() / "jobs.db"


@dataclass(slots=True)
class AppConfig:
    """Everything the user can change that is not a one-off job detail."""

    sync: SyncOptions = field(default_factory=SyncOptions)
    theme: Theme = Theme.AUTOMATIC
    concurrency: int = field(default_factory=default_concurrency)
    match_threshold: float = 75.0
    recursive_scan: bool = True
    output_suffix: str = ".synced"
    speech_cache_enabled: bool = True
    history_limit: int = 2000
    # Start draining as soon as work is queued, rather than waiting for a click.
    auto_start: bool = True
    # Default state of the per-batch "sample the audio" toggle.
    fast_mode: bool = False

    def normalized(self) -> Self:
        """Clamp values a hand-edited config file could put out of range."""
        self.concurrency = max(1, min(MAX_CONCURRENCY, int(self.concurrency)))
        self.match_threshold = max(0.0, min(100.0, float(self.match_threshold)))
        self.history_limit = max(0, int(self.history_limit))
        suffix = self.output_suffix.strip()
        if suffix and not suffix.startswith("."):
            suffix = f".{suffix}"
        # An empty suffix would make the output overwrite its own input.
        self.output_suffix = suffix or ".synced"
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync": self.sync.to_dict(),
            "theme": self.theme.value,
            "concurrency": self.concurrency,
            "match_threshold": self.match_threshold,
            "recursive_scan": self.recursive_scan,
            "output_suffix": self.output_suffix,
            "speech_cache_enabled": self.speech_cache_enabled,
            "history_limit": self.history_limit,
            "auto_start": self.auto_start,
            "fast_mode": self.fast_mode,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            return cls().normalized()
        default = cls()
        config = cls(
            sync=SyncOptions.from_dict(value.get("sync")),
            theme=_theme(value.get("theme"), default.theme),
            concurrency=_int(value.get("concurrency"), default.concurrency),
            match_threshold=_float(
                value.get("match_threshold"), default.match_threshold
            ),
            recursive_scan=bool(value.get("recursive_scan", default.recursive_scan)),
            output_suffix=str(value.get("output_suffix", default.output_suffix)),
            speech_cache_enabled=bool(
                value.get("speech_cache_enabled", default.speech_cache_enabled)
            ),
            history_limit=_int(value.get("history_limit"), default.history_limit),
            auto_start=bool(value.get("auto_start", default.auto_start)),
            fast_mode=bool(value.get("fast_mode", default.fast_mode)),
        )
        return config.normalized()

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        target = path or config_path()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A missing or corrupt config must never stop the app starting.
            return cls().normalized()
        return cls.from_dict(payload)

    def save(self, path: Path | None = None) -> None:
        target = path or config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        # Write-then-rename so a crash mid-save cannot truncate the config.
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=".config-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, target)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise


def _theme(value: object, default: Theme) -> Theme:
    try:
        return Theme(value)
    except ValueError:
        # Unknown or hand-edited value; fall back rather than fail to start.
        return default


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
