"""Shared fixtures.

The proof of concept had none, so every test rebuilt its own scaffolding. These
exist so a test can say what it is about in one line.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import pytest

from ffsubsyncui.core.config import AppConfig
from ffsubsyncui.core.models import SyncJob, SyncOptions, default_output_for
from ffsubsyncui.core.store import JobStore


@pytest.fixture
def media_tree(tmp_path: Path) -> Callable[..., Path]:
    """Create empty files with the given names and return their folder.

    Matching is purely filename-driven, so empty files are enough for every
    test that is not about running a real sync.
    """

    def build(names: Iterable[str], *, subfolder: str = "") -> Path:
        folder = tmp_path / subfolder if subfolder else tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        return folder

    return build


@pytest.fixture
def store(tmp_path: Path) -> Iterator[JobStore]:
    instance = JobStore(tmp_path / "jobs.db")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(sync=SyncOptions(vad="auditok"), concurrency=1)


@pytest.fixture
def make_job(tmp_path: Path) -> Callable[..., SyncJob]:
    """A job whose reference and subtitle really exist on disk."""

    def build(
        name: str = "Show.S01E01",
        *,
        options: SyncOptions | None = None,
        group_key: str | None = None,
    ) -> SyncJob:
        reference = tmp_path / f"{name}.mkv"
        subtitle = tmp_path / f"{name}.en.srt"
        reference.touch()
        subtitle.touch()
        return SyncJob(
            reference_path=reference,
            subtitle_path=subtitle,
            output_path=default_output_for(subtitle),
            options=options or SyncOptions(),
            group_key=group_key,
        )

    return build
