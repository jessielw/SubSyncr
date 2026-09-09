"""Qt test setup: headless, and never touching the real user data directory."""

from __future__ import annotations

import os

# Must be set before Qt creates a platform integration.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Redirect config, database and speech cache into the test's tmp_path."""
    target = tmp_path / "appdata"
    monkeypatch.setenv("SUBSYNCR_DATA_DIR", str(target))
    yield target
