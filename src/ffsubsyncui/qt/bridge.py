"""The single seam between the UI-free core and Qt.

``SyncQueue`` invokes its callbacks on worker threads. Emitting a signal from a
non-GUI thread is safe - Qt falls back to a queued connection automatically -
so this adapter is the only marshalling the application needs.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ffsubsyncui.core.models import SyncJob
from ffsubsyncui.core.queue import QueueCallbacks, SyncQueue


class QueueBridge(QObject):
    """Re-emits core queue callbacks as Qt signals on the GUI thread."""

    # Job ids rather than job objects: the core owns the job, and passing the
    # mutable instance across threads would invite reading it mid-update.
    job_changed = Signal(str)
    progress = Signal(str, object, str)
    log = Signal(str, str, str)
    running_changed = Signal(bool)
    queue_changed = Signal()

    def __init__(self, queue: SyncQueue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        queue.callbacks.job_changed = self._on_job_changed
        queue.callbacks.progress = self._on_progress
        queue.callbacks.log = self._on_log
        queue.callbacks.running_changed = self._on_running_changed
        queue.callbacks.queue_changed = self._on_queue_changed

    def detach(self) -> None:
        """Stop forwarding, so a shutting-down window is never signalled."""
        self.queue.callbacks = QueueCallbacks()

    def _on_job_changed(self, job: SyncJob) -> None:
        self.job_changed.emit(job.id)

    def _on_progress(self, job_id: str, fraction: float | None, stage: str) -> None:
        self.progress.emit(job_id, fraction, stage)

    def _on_log(self, job_id: str, level: str, message: str) -> None:
        self.log.emit(job_id, level, message)

    def _on_running_changed(self, running: bool) -> None:
        self.running_changed.emit(running)

    def _on_queue_changed(self) -> None:
        self.queue_changed.emit()
