"""The last line of defence: exceptions nobody caught reaching the user."""

from __future__ import annotations

from collections.abc import Iterator
import sys
import threading
from types import TracebackType

from PySide6.QtWidgets import QMessageBox
import pytest

from subsyncr.qt.errors import ErrorReporter


def _failure(
    message: str = "kaboom",
) -> tuple[type[BaseException], BaseException, TracebackType | None]:
    try:
        raise RuntimeError(message)
    except RuntimeError:
        kind, value, tb = sys.exc_info()
        assert kind is not None and value is not None
        return kind, value, tb


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> list[QMessageBox]:
    """Catch the dialogs instead of blocking the test on a modal loop."""
    shown: list[QMessageBox] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box) or 0)
    return shown


@pytest.fixture
def reporter(qapp) -> ErrorReporter:
    return ErrorReporter()


@pytest.fixture
def fresh_hooks(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Let install() run for real, and put the process's hooks back after."""
    chained: list[object] = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: chained.append(args))
    monkeypatch.setattr(threading, "excepthook", lambda args: chained.append(args))
    monkeypatch.setattr(ErrorReporter, "_installed", None)
    yield chained


def test_a_failure_reaches_a_dialog(
    qtbot, reporter: ErrorReporter, boxes: list[QMessageBox]
) -> None:
    reporter.report(*_failure())
    qtbot.waitUntil(lambda: bool(boxes))
    assert "RuntimeError: kaboom" in boxes[0].informativeText()
    # The traceback is there for whoever has to fix it, folded away.
    assert "_failure" in boxes[0].detailedText()


def test_the_hook_keeps_the_console_report(
    qtbot, boxes: list[QMessageBox], fresh_hooks: list[object]
) -> None:
    ErrorReporter.install()
    sys.excepthook(*_failure())
    assert fresh_hooks, "the replaced hook was dropped rather than chained"
    qtbot.waitUntil(lambda: bool(boxes))


def test_a_deliberate_exit_is_not_an_error(
    qapp, boxes: list[QMessageBox], fresh_hooks: list[object]
) -> None:
    ErrorReporter.install()
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    qapp.processEvents()
    assert boxes == []


def test_a_crashing_thread_is_reported_too(
    qtbot, boxes: list[QMessageBox], fresh_hooks: list[object]
) -> None:
    ErrorReporter.install()

    def explode() -> None:
        raise RuntimeError("off the GUI thread")

    thread = threading.Thread(target=explode, name="subsyncr-test")
    thread.start()
    thread.join()

    qtbot.waitUntil(lambda: bool(boxes))
    assert "off the GUI thread" in boxes[0].informativeText()
    assert "subsyncr-test" in boxes[0].informativeText()


def test_one_dialog_at_a_time(
    qtbot, reporter: ErrorReporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[QMessageBox] = []

    def reenter(box: QMessageBox) -> int:
        shown.append(box)
        # A widget that raises while repainting keeps raising; the dialog must
        # not breed while it is on screen.
        reporter._show("second", "second")
        return 0

    monkeypatch.setattr(QMessageBox, "exec", reenter)
    reporter.report(*_failure())
    qtbot.waitUntil(lambda: bool(shown))
    assert len(shown) == 1
