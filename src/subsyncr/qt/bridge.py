"""The single seam between the UI-free core and Qt.

``SyncQueue`` invokes its callbacks on worker threads. Rather than turn every
one of them into its own signal, the bridge collects them and delivers them to
the GUI thread in batches, at most once per ``FLUSH_INTERVAL_MS``.

A busy batch produces events far faster than anyone can read them: progress
for every member of every running group, a log line per engine message, a
job update per finished subtitle. Delivered one at a time, each cost a table
repaint and a status recount, and a long queue spent all its time on that
backlog - the window stopped answering. Batched, the GUI does one pass per
tick however much happened in between.

Events raised on the GUI thread itself (adding jobs, ticking a checkbox) are
delivered at once, so the window reflects a click before the call returns.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from subsyncr.core.models import SyncJob
from subsyncr.core.queue import QueueCallbacks, SyncQueue

#: How often queued-up worker events reach the GUI.
FLUSH_INTERVAL_MS = 100

#: Log lines kept per delivery. The log file has every line; the pane only
#: needs the recent ones, and a runaway tool should not stall the window.
MAX_LOG_LINES_PER_FLUSH = 1000

LogEntry = tuple[str, str, str]


class QueueBridge(QObject):
    """Re-emits core queue callbacks as batched Qt signals on the GUI thread."""

    # Job ids rather than job objects: the core owns the job, and passing the
    # mutable instance across threads would invite reading it mid-update.
    queue_changed = Signal()
    #: Ids whose rows changed in any way.
    jobs_changed = Signal(list)
    #: Ids whose progress alone moved.
    progress = Signal(list)
    #: ``(job_id, level, message)`` tuples, oldest first.
    logs = Signal(list)
    #: Lines dropped from this delivery to honour ``MAX_LOG_LINES_PER_FLUSH``.
    logs_dropped = Signal(int)
    running_changed = Signal(bool)
    #: After every delivery, once the signals above have been handled.
    flushed = Signal()

    # Internal: a worker thread asking the GUI thread to start the timer.
    _wake = Signal()

    def __init__(self, queue: SyncQueue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self._lock = threading.Lock()
        self._structure = False
        self._jobs: dict[str, None] = {}
        self._progress: dict[str, None] = {}
        self._logs: list[LogEntry] = []
        self._dropped = 0
        self._running: bool | None = None
        self._reported_running: bool | None = None
        self._scheduled = False
        self._flushing = False
        self._detached = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(FLUSH_INTERVAL_MS)
        self._timer.timeout.connect(self.flush)
        # Cross-thread, so queued: the timer must be started on its own thread.
        self._wake.connect(self._start_timer)

        queue.callbacks.job_changed = self._on_job_changed
        queue.callbacks.progress = self._on_progress
        queue.callbacks.log = self._on_log
        queue.callbacks.running_changed = self._on_running_changed
        queue.callbacks.queue_changed = self._on_queue_changed

    def detach(self) -> None:
        """Stop forwarding, so a shutting-down window is never signalled."""
        self.queue.callbacks = QueueCallbacks()
        with self._lock:
            self._detached = True
            self._clear()
        self._timer.stop()

    # -- core callbacks, on any thread ---------------------------------

    def _on_job_changed(self, job: SyncJob) -> None:
        with self._lock:
            self._jobs[job.id] = None
        self._schedule()

    def _on_progress(self, job_id: str, fraction: float | None, stage: str) -> None:
        del fraction, stage  # The job object already holds the current values.
        with self._lock:
            self._progress[job_id] = None
        self._schedule()

    def _on_log(self, job_id: str, level: str, message: str) -> None:
        with self._lock:
            self._logs.append((job_id, level, message))
            if len(self._logs) > MAX_LOG_LINES_PER_FLUSH:
                # Keep the newest; the oldest are in the log file anyway.
                excess = len(self._logs) - MAX_LOG_LINES_PER_FLUSH
                del self._logs[:excess]
                self._dropped += excess
        self._schedule()

    def _on_running_changed(self, running: bool) -> None:
        with self._lock:
            self._running = running
        self._schedule()

    def _on_queue_changed(self) -> None:
        with self._lock:
            self._structure = True
        self._schedule()

    def _schedule(self) -> None:
        if QThread.currentThread() == self.thread():
            # A direct call: deliver now, so the caller sees its own change.
            self.flush()
            return
        with self._lock:
            if self._scheduled or self._detached:
                return
            self._scheduled = True
        self._wake.emit()

    # -- GUI thread ----------------------------------------------------

    @Slot()
    def _start_timer(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    @Slot()
    def flush(self) -> None:
        """Deliver everything collected since the last delivery."""
        if self._flushing:
            # A slot changed the queue mid-delivery; pick that up next tick.
            self._start_timer()
            return
        with self._lock:
            if self._detached:
                return
            structure = self._structure
            jobs = list(self._jobs)
            progress = [job_id for job_id in self._progress if job_id not in self._jobs]
            logs = self._logs
            dropped = self._dropped
            running = self._running
            self._clear()
        if not (structure or jobs or progress or logs or running is not None):
            return
        self._flushing = True
        try:
            # Rows first, so every later signal can find its job's row.
            if structure:
                self.queue_changed.emit()
            if jobs:
                self.jobs_changed.emit(jobs)
            if progress:
                self.progress.emit(progress)
            if dropped:
                self.logs_dropped.emit(dropped)
            if logs:
                self.logs.emit(logs)
            if running is not None and running != self._reported_running:
                self._reported_running = running
                self.running_changed.emit(running)
            self.flushed.emit()
        finally:
            self._flushing = False

    def _clear(self) -> None:
        """Forget what is pending. Call with ``_lock`` held."""
        self._structure = False
        self._jobs = {}
        self._progress = {}
        self._logs = []
        self._dropped = 0
        self._running = None
        self._scheduled = False
