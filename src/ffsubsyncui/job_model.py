from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor

from ffsubsyncui.domain import JobStatus, SyncJob, SyncResult


class JobTableModel(QAbstractTableModel):
    persistence_changed = Signal()

    HEADERS = ("Run", "Status", "Reference", "Subtitle", "Output", "Progress", "Result")

    def __init__(
        self, jobs: Iterable[SyncJob] = (), parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.jobs = list(jobs)

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.jobs)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        job = self.jobs[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return Qt.CheckState.Checked if job.enabled else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(job, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(job, column)
        if role == Qt.ItemDataRole.ForegroundRole and column == 1:
            colors = {
                JobStatus.QUEUED: QColor("#6b7280"),
                JobStatus.RUNNING: QColor("#2563eb"),
                JobStatus.SUCCEEDED: QColor("#15803d"),
                JobStatus.FAILED: QColor("#b91c1c"),
            }
            return colors[job.status]
        if role == Qt.ItemDataRole.UserRole:
            return job.id
        return None

    def _display(self, job: SyncJob, column: int) -> str:
        if column == 0:
            return ""
        if column == 1:
            return job.status.value.title()
        if column == 2:
            return job.reference_path.name
        if column == 3:
            return job.subtitle_path.name
        if column == 4:
            return job.output_path.name
        if column == 5:
            if job.status is JobStatus.RUNNING and job.progress is None:
                return job.stage
            if job.progress is not None:
                return f"{job.progress:.0%} — {job.stage}"
            return job.stage
        if column == 6:
            if job.result and job.result.success:
                offset = job.result.offset_seconds
                return "Complete" if offset is None else f"Offset {offset:+.3f}s"
            return job.last_error
        return ""

    @staticmethod
    def _tooltip(job: SyncJob, column: int) -> str:
        if column == 2:
            return str(job.reference_path)
        if column == 3:
            return str(job.subtitle_path)
        if column == 4:
            return str(job.output_path)
        if column == 6:
            return job.last_error
        return ""

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if (
            index.isValid()
            and index.column() == 0
            and self.jobs[index.row()].status is JobStatus.QUEUED
        ):
            flags |= Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            index.isValid()
            and index.column() == 0
            and role == Qt.ItemDataRole.CheckStateRole
            and self.jobs[index.row()].status is JobStatus.QUEUED
        ):
            self.jobs[index.row()].enabled = value in (
                Qt.CheckState.Checked,
                Qt.CheckState.Checked.value,
            )
            self.dataChanged.emit(index, index, [role])
            self.persistence_changed.emit()
            return True
        return False

    def add_jobs(self, jobs: Iterable[SyncJob]) -> None:
        additions = list(jobs)
        if not additions:
            return
        first = len(self.jobs)
        self.beginInsertRows(QModelIndex(), first, first + len(additions) - 1)
        self.jobs.extend(additions)
        self.endInsertRows()
        self.persistence_changed.emit()

    def remove_rows(self, rows: Iterable[int]) -> None:
        for row in sorted(set(rows), reverse=True):
            if (
                0 <= row < len(self.jobs)
                and self.jobs[row].status is not JobStatus.RUNNING
            ):
                self.beginRemoveRows(QModelIndex(), row, row)
                del self.jobs[row]
                self.endRemoveRows()
        self.persistence_changed.emit()

    def clear_completed(self) -> None:
        self.remove_rows(
            index
            for index, job in enumerate(self.jobs)
            if job.status is JobStatus.SUCCEEDED
        )

    def retry_rows(self, rows: Iterable[int]) -> None:
        changed = False
        for row in set(rows):
            if 0 <= row < len(self.jobs) and self.jobs[row].status is JobStatus.FAILED:
                job = self.jobs[row]
                job.status = JobStatus.QUEUED
                job.stage = "Waiting"
                job.last_error = ""
                job.result = None
                job.progress = None
                changed = True
                self._emit_row(row)
        if changed:
            self.persistence_changed.emit()

    def next_runnable(self) -> SyncJob | None:
        return next(
            (
                job
                for job in self.jobs
                if job.enabled and job.status is JobStatus.QUEUED
            ),
            None,
        )

    def mark_running(self, job_id: str) -> None:
        if (entry := self._find(job_id)) is None:
            return
        row, job = entry
        job.status = JobStatus.RUNNING
        job.progress = None
        job.stage = "Starting"
        job.last_error = ""
        self._emit_row(row)
        self.persistence_changed.emit()

    def update_progress(self, job_id: str, progress: float | None, stage: str) -> None:
        if (entry := self._find(job_id)) is None:
            return
        row, job = entry
        job.progress = progress
        job.stage = stage
        self._emit_row(row, 5, 5)

    def finish(self, job_id: str, result: SyncResult) -> None:
        if (entry := self._find(job_id)) is None:
            return
        row, job = entry
        job.result = result
        job.progress = 1.0 if result.success else None
        job.status = JobStatus.SUCCEEDED if result.success else JobStatus.FAILED
        job.stage = "Complete" if result.success else "Failed"
        job.last_error = result.error
        self._emit_row(row)
        self.persistence_changed.emit()

    def counts(self) -> dict[JobStatus, int]:
        return {
            status: sum(job.status is status for job in self.jobs)
            for status in JobStatus
        }

    def _find(self, job_id: str) -> tuple[int, SyncJob] | None:
        return next(
            ((index, job) for index, job in enumerate(self.jobs) if job.id == job_id),
            None,
        )

    def _emit_row(self, row: int, first: int = 0, last: int | None = None) -> None:
        end = self.columnCount() - 1 if last is None else last
        self.dataChanged.emit(self.index(row, first), self.index(row, end))
