"""Table model and view for the active job queue."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

from ffsubsyncui.core.models import JobStatus, SyncJob
from ffsubsyncui.core.queue import SyncQueue

_STATUS_COLORS = {
    JobStatus.QUEUED: QColor("#6b7280"),
    JobStatus.RUNNING: QColor("#2563eb"),
    JobStatus.SUCCEEDED: QColor("#15803d"),
    JobStatus.FAILED: QColor("#b91c1c"),
    JobStatus.CANCELLED: QColor("#78716c"),
    JobStatus.SKIPPED_LOW_QUALITY: QColor("#b45309"),
}

_STATUS_LABELS = {
    JobStatus.QUEUED: "Queued",
    JobStatus.RUNNING: "Running",
    JobStatus.SUCCEEDED: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
    JobStatus.SKIPPED_LOW_QUALITY: "Low quality",
}

COLUMN_RUN = 0
COLUMN_STATUS = 1
COLUMN_REFERENCE = 2
COLUMN_SUBTITLE = 3
COLUMN_LANGUAGE = 4
COLUMN_PROGRESS = 5
COLUMN_RESULT = 6


class JobTableModel(QAbstractTableModel):
    """A read-mostly mirror of ``SyncQueue``; the queue stays the source of truth."""

    HEADERS = (
        "Run",
        "Status",
        "Reference",
        "Subtitle",
        "Language",
        "Progress",
        "Result",
    )

    def __init__(self, queue: SyncQueue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue = queue
        self._jobs: list[SyncJob] = queue.snapshot()
        self._rows: dict[str, int] = {job.id: i for i, job in enumerate(self._jobs)}

    # -- syncing with the core -------------------------------------------

    def refresh(self) -> None:
        """Rebuild from the queue after jobs are added, removed or reordered."""
        self.beginResetModel()
        self._jobs = self._queue.snapshot()
        self._rows = {job.id: i for i, job in enumerate(self._jobs)}
        self.endResetModel()

    def touch(self, job_id: str, first: int = 0, last: int | None = None) -> None:
        """Repaint one row whose underlying job mutated in place."""
        row = self._rows.get(job_id)
        if row is None or row >= len(self._jobs):
            return
        end = self.columnCount() - 1 if last is None else last
        self.dataChanged.emit(self.index(row, first), self.index(row, end))

    def job_at(self, row: int) -> SyncJob | None:
        return self._jobs[row] if 0 <= row < len(self._jobs) else None

    def jobs_for_rows(self, rows: list[int]) -> list[SyncJob]:
        return [self._jobs[row] for row in rows if 0 <= row < len(self._jobs)]

    # -- QAbstractTableModel ---------------------------------------------

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._jobs)

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
        job = self._jobs[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and column == COLUMN_RUN:
            return Qt.CheckState.Checked if job.enabled else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(job, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(job, column)
        if role == Qt.ItemDataRole.ForegroundRole and column == COLUMN_STATUS:
            return _STATUS_COLORS.get(job.status)
        if role == Qt.ItemDataRole.UserRole:
            return job.id
        return None

    @staticmethod
    def _display(job: SyncJob, column: int) -> str:
        match column:
            case _ if column == COLUMN_RUN:
                return ""
            case _ if column == COLUMN_STATUS:
                return _STATUS_LABELS.get(job.status, job.status.value)
            case _ if column == COLUMN_REFERENCE:
                return job.reference_path.name
            case _ if column == COLUMN_SUBTITLE:
                return job.subtitle_path.name
            case _ if column == COLUMN_LANGUAGE:
                return job.language or ""
            case _ if column == COLUMN_PROGRESS:
                if job.status is JobStatus.RUNNING:
                    if job.progress is None:
                        return job.stage
                    return f"{job.progress:.0%} - {job.stage}"
                # Status already names the outcome, so spend this column on
                # how long it took instead of repeating it.
                if job.result is not None and job.result.duration_seconds:
                    return f"{job.result.duration_seconds:.1f}s"
                return job.stage
            case _ if column == COLUMN_RESULT:
                return _result_text(job)
        return ""

    @staticmethod
    def _tooltip(job: SyncJob, column: int) -> str:
        match column:
            case _ if column == COLUMN_REFERENCE:
                return str(job.reference_path)
            case _ if column == COLUMN_SUBTITLE:
                return str(job.subtitle_path)
            case _ if column == COLUMN_RESULT:
                parts: list[str] = []
                if job.last_error:
                    parts.append(job.last_error)
                if job.result is not None:
                    if job.result.score is not None:
                        parts.append(f"alignment score {job.result.score:.1f}")
                    if job.result.shared_extraction:
                        parts.append("reused a sibling's extraction")
                    elif job.result.used_speech_cache:
                        parts.append("reused cached speech")
                    if job.result.duration_seconds:
                        parts.append(f"took {job.result.duration_seconds:.1f}s")
                parts.append(f"output: {job.effective_output}")
                return "\n".join(parts)
        return ""

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if (
            index.isValid()
            and index.column() == COLUMN_RUN
            and self._jobs[index.row()].status is JobStatus.QUEUED
        ):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            not index.isValid()
            or index.column() != COLUMN_RUN
            or role != Qt.ItemDataRole.CheckStateRole
        ):
            return False
        job = self._jobs[index.row()]
        enabled = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
        return self._queue.set_enabled(job.id, enabled)


def _result_text(job: SyncJob) -> str:
    result = job.result
    if result is None:
        return job.last_error
    if result.success:
        pieces: list[str] = []
        if result.offset_seconds is not None:
            pieces.append(f"offset {result.offset_seconds:+.3f}s")
        if (
            result.framerate_scale_factor is not None
            and abs(result.framerate_scale_factor - 1.0) > 0.0005
        ):
            pieces.append(f"rate x{result.framerate_scale_factor:.4f}")
        if result.shared_extraction:
            pieces.append("shared")
        elif result.used_speech_cache:
            pieces.append("cached")
        return ", ".join(pieces) if pieces else "Complete"
    if job.status is JobStatus.SKIPPED_LOW_QUALITY:
        reasons = "; ".join(result.quality_reasons) or result.error
        return f"Left unmodified ({reasons})" if reasons else "Left unmodified"
    return result.error or job.last_error


class JobTableView(QTableView):
    def __init__(self, model: JobTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        for column in (
            COLUMN_RUN,
            COLUMN_STATUS,
            COLUMN_LANGUAGE,
            COLUMN_PROGRESS,
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (COLUMN_REFERENCE, COLUMN_SUBTITLE, COLUMN_RESULT):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

    def selected_rows(self) -> list[int]:
        selection = self.selectionModel()
        if selection is None:
            return []
        return sorted({index.row() for index in selection.selectedRows()})
