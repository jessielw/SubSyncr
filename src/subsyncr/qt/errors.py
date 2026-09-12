"""What the application does with an exception nobody caught.

Qt calls into Python from C++, so an exception raised inside a slot unwinds
into the event loop rather than out of ``main``: the interpreter prints a
traceback to stderr and carries on as if nothing happened. In a windowed build
there is no stderr to read, and the only symptom is that whatever the user
asked for silently did not happen - a queue that says "Waiting" forever. These
hooks put the traceback in front of the user instead.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import traceback
from types import TracebackType

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox

_TITLE = "Unexpected error"


class ErrorReporter(QObject):
    """Turns an exception into a dialog, from whichever thread raised it."""

    # Marshalling is the whole point: the queue's workers are plain threads,
    # and a widget may only be touched from the GUI thread.
    reported = Signal(str, str)

    # The one reporter the process's hooks feed, once install() has run. It
    # lives on the class because the thing it guards - sys.excepthook - is
    # process-wide too, and a second set of hooks would mean two dialogs per
    # failure.
    _installed: ErrorReporter | None = None

    def __init__(self) -> None:
        super().__init__()
        self._showing = False
        # Queued even when the emitter is the GUI thread, because the stack
        # that raised may be a paint event or a native drag loop. Opening a
        # modal dialog there nests an event loop inside code that is halfway
        # through unwinding; the queued connection waits for it to finish.
        self.reported.connect(self._show, Qt.ConnectionType.QueuedConnection)

    def report(
        self,
        kind: type[BaseException],
        value: BaseException,
        tb: TracebackType | None,
        thread: threading.Thread | None = None,
    ) -> None:
        summary = f"{kind.__name__}: {value}" if str(value) else kind.__name__
        if thread is not None:
            summary = f"{summary}\n(in background thread {thread.name})"
        self.reported.emit(summary, "".join(traceback.format_exception(value)))

    @Slot(str, str)
    def _show(self, summary: str, detail: str) -> None:
        if self._showing:
            # A bug in a repainting widget raises once a frame. One dialog is
            # a report; fifty stacked dialogs are a second failure.
            return
        self._showing = True
        try:
            box = QMessageBox(QApplication.activeWindow())
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle(_TITLE)
            box.setText(
                "Something went wrong inside SubSyncr, and the last action did "
                "not finish."
            )
            box.setInformativeText(summary)
            box.setDetailedText(detail)
            box.exec()
        finally:
            self._showing = False

    @classmethod
    def install(cls) -> ErrorReporter:
        """Route unhandled exceptions, on any thread, into a dialog.

        Both hooks still chain to the ones they replace, so the console report
        a developer is watching for is never traded away for the dialog.
        """
        if cls._installed is not None:
            return cls._installed
        reporter = cls()
        previous = sys.excepthook
        previous_threaded = threading.excepthook

        def excepthook(
            kind: type[BaseException], value: BaseException, tb: TracebackType | None
        ) -> None:
            # A windowed build may have no usable stderr at all; whatever that
            # costs the console, the dialog still has to happen.
            with contextlib.suppress(Exception):
                previous(kind, value, tb)
            # Ctrl+C and a deliberate exit are how a program ends, not a fault.
            if not issubclass(kind, (KeyboardInterrupt, SystemExit)):
                reporter.report(kind, value, tb)

        def threading_excepthook(args: threading.ExceptHookArgs) -> None:
            with contextlib.suppress(Exception):
                previous_threaded(args)
            if args.exc_value is None or issubclass(args.exc_type, SystemExit):
                return
            reporter.report(
                args.exc_type, args.exc_value, args.exc_traceback, args.thread
            )

        sys.excepthook = excepthook
        threading.excepthook = threading_excepthook
        cls._installed = reporter
        return reporter
