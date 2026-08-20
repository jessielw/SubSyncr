from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ffsubsyncui.domain import SyncJob, SyncResult
from ffsubsyncui.job_model import JobTableModel
from ffsubsyncui.runner import FFSubsyncRunner, RunnerError


class JobWorker(QObject):
    progress = Signal(str, object, str)
    log = Signal(str, str)
    finished = Signal(str, object)

    def __init__(self, job: SyncJob) -> None:
        super().__init__()
        self.job = job

    @Slot()
    def run(self) -> None:
        runner = FFSubsyncRunner()
        try:
            result = runner.run(
                self.job,
                progress_callback=lambda value, stage: self.progress.emit(
                    self.job.id, value, stage
                ),
                log_callback=lambda message: self.log.emit(self.job.id, message),
            )
        except RunnerError as exc:
            result = SyncResult(False, 1, error=str(exc))
        self.finished.emit(self.job.id, result)


class QueueController(QObject):
    running_changed = Signal(bool)
    job_log = Signal(str, str)

    def __init__(self, model: JobTableModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self._draining = False
        self._thread: QThread | None = None
        self._worker: JobWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    @Slot()
    def start(self) -> None:
        self._draining = True
        if self._thread is None:
            self._start_next()

    @Slot()
    def stop_after_current(self) -> None:
        self._draining = False
        if self._thread is None:
            self.running_changed.emit(False)

    def _start_next(self) -> None:
        if not self._draining:
            self.running_changed.emit(False)
            return
        job = self.model.next_runnable()
        if job is None:
            self._draining = False
            self.running_changed.emit(False)
            return

        self.model.mark_running(job.id)
        thread = QThread(self)
        worker = JobWorker(job)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.model.update_progress)
        worker.log.connect(self.job_log)
        worker.finished.connect(self._job_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self.running_changed.emit(True)
        thread.start()

    @Slot(str, object)
    def _job_finished(self, job_id: str, result: object) -> None:
        if isinstance(result, SyncResult):
            self.model.finish(job_id, result)
        else:
            self.model.finish(
                job_id, SyncResult(False, 1, error="Unknown worker result")
            )

    @Slot()
    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._start_next()
