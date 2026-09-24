"""Console windows stay hidden, including for tools we do not call ourselves.

The frozen worker is a windowed executable, so a console program it starts
without ``CREATE_NO_WINDOW`` gets a window of its own. The engines we drive
in-process spawn ffmpeg on their own terms, which is why the guard is global
rather than a keyword argument at each call site.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
import subprocess
import sys

import pytest

from subsyncr.core import spawn
from subsyncr.core.spawn import ConsoleGuard

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="console windows are a Windows problem"
)

# Reports the console window handle of whatever process runs it. Zero means the
# process has no console at all, which is what CREATE_NO_WINDOW buys.
_REPORT_CONSOLE = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"


@pytest.fixture
def guard_installed() -> Iterator[None]:
    """Install the guard, and take it back out again afterwards.

    It patches ``subprocess.Popen`` for the whole interpreter, so leaving it in
    place would quietly change every later test in the session.
    """
    ConsoleGuard.install()
    try:
        yield
    finally:
        ConsoleGuard.uninstall()


def test_our_own_spawns_ask_for_no_window() -> None:
    expected = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    assert spawn.creation_flags() == expected


@windows_only
def test_a_plain_spawn_is_given_no_window() -> None:
    assert spawn.hide_window() == subprocess.CREATE_NO_WINDOW


@windows_only
def test_unrelated_flags_are_kept() -> None:
    flags = spawn.hide_window(subprocess.CREATE_NEW_PROCESS_GROUP)
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert flags & subprocess.CREATE_NO_WINDOW


@windows_only
@pytest.mark.parametrize(
    # By name, not value: parametrize runs at import, before skipif can act,
    # and neither constant exists in subprocess off Windows.
    "chosen",
    ["CREATE_NEW_CONSOLE", "DETACHED_PROCESS"],
    ids=["new-console", "detached"],
)
def test_a_caller_that_chose_a_console_is_not_overruled(chosen: str) -> None:
    """Both are incompatible with CREATE_NO_WINDOW; adding it would raise."""
    flag = getattr(subprocess, chosen)
    assert spawn.hide_window(flag) == flag


@windows_only
def test_startupinfo_is_left_to_handle_its_own_window() -> None:
    """ffsubsync's ffmpeg calls hide themselves this way already."""
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    assert spawn.hide_window(0, info) == 0


@windows_only
@pytest.mark.usefixtures("guard_installed")
def test_a_library_that_passes_no_flags_still_gets_no_console() -> None:
    """The autosubsync and ffsubsync ffmpeg calls look exactly like this."""
    output = subprocess.run(
        [sys.executable, "-c", _REPORT_CONSOLE],
        capture_output=True,
        text=True,
        check=True,
    )
    assert output.stdout.strip() == "0"


@windows_only
@pytest.mark.usefixtures("guard_installed")
def test_positional_arguments_are_understood() -> None:
    """Popen's window arguments can arrive positionally; binding catches those."""
    process = subprocess.Popen(
        [sys.executable, "-c", _REPORT_CONSOLE],
        -1,
        None,
        None,
        subprocess.PIPE,
        None,
        None,
        True,
    )
    stdout, _ = process.communicate()
    assert process.returncode == 0
    assert stdout.strip() == b"0"


@windows_only
def test_installing_twice_does_not_stack_wrappers(guard_installed: None) -> None:
    del guard_installed
    patched = subprocess.Popen.__init__
    ConsoleGuard.install()
    assert subprocess.Popen.__init__ is patched


@windows_only
def test_uninstalling_leaves_no_trace(guard_installed: None) -> None:
    """The saved function is the guard's only record of the untouched Popen."""
    del guard_installed
    assert ConsoleGuard.installed()
    ConsoleGuard.uninstall()
    assert not ConsoleGuard.installed()
    ConsoleGuard.uninstall()  # Twice is not an error either.


def test_install_is_inert_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConsoleGuard, "_original", None)
    monkeypatch.setattr(os, "name", "posix")
    original = subprocess.Popen.__init__
    ConsoleGuard.install()
    assert subprocess.Popen.__init__ is original
    assert not ConsoleGuard.installed()
    assert spawn.creation_flags() == 0
