"""The GUI's ownership lock survives long runs and recovers dead owners."""

from pathlib import Path
import subprocess
import sys

import pytest

from subsyncr.qt.instance import acquire_gui_lock


def test_second_window_is_refused_until_owner_releases(tmp_path: Path):
    first = acquire_gui_lock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already open"):
            acquire_gui_lock(tmp_path)
        other = acquire_gui_lock(tmp_path / "other-profile")
        other.unlock()
    finally:
        first.unlock()
    replacement = acquire_gui_lock(tmp_path)
    replacement.unlock()


def test_crashed_owner_is_recovered(tmp_path: Path):
    script = (
        "from pathlib import Path; import os, sys; "
        "from subsyncr.qt.instance import acquire_gui_lock; "
        "lock = acquire_gui_lock(Path(sys.argv[1])); os._exit(0)"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], check=True, timeout=20
    )
    lock = acquire_gui_lock(tmp_path)
    try:
        assert lock.isLocked()
        assert lock.staleLockTime() == 0
    finally:
        lock.unlock()
