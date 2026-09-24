"""Table model and view for the active job queue."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Slot,
)
from PySide6.QtGui import QIcon, QPainter, QPaintEvent, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from subsyncr.core.models import JobStatus, SyncJob
from subsyncr.core.queue import SyncQueue
from subsyncr.qt.icons import STATUS_ICONS
from subsyncr.qt.palette import status_color
from subsyncr.qt.theme import ThemeManager
from subsyncr.qt.widgets import enable_row_stripes

_STATUS_LABELS = {
    JobStatus.QUEUED: "Queued",
    JobStatus.RUNNING: "Running",
    JobStatus.SUCCEEDED: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
    JobStatus.SKIPPED_LOW_QUALITY: "Low quality",
}

#: The raw 0..1 fraction, for the delegate. DisplayRole stays human-readable.
ROLE_PROGRESS = Qt.ItemDataRole.UserRole + 1

# QStyleOptionProgressBar is integer-valued; this is just the resolution the
# fraction is quantised to.
_PROGRESS_STEPS = 1000

# Room either side of the elided label so it does not touch the bar's border.
_PROGRESS_TEXT_PADDING = 8

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
        # Status icons are tinted for one colour scheme, so they are cached
        # until the theme says otherwise rather than rebuilt per paint.
        self._icons: dict[JobStatus, QIcon] = {}
        self._theme = ThemeManager.for_app()
        if self._theme is not None:
            self._theme.changed.connect(self._theme_changed)

    @Slot()
    def _theme_changed(self) -> None:
        self._icons.clear()
        if self._jobs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._jobs) - 1, self.columnCount() - 1),
            )

    def _status_icon(self, status: JobStatus) -> QIcon | None:
        if self._theme is None:
            return None
        cached = self._icons.get(status)
        if cached is None:
            icon = STATUS_ICONS.get(status)
            color = status_color(status)
            if icon is None or color is None:
                return None
            cached = self._theme.icon(icon, color.name())
            self._icons[status] = cached
        return cached

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
        if role == Qt.ItemDataRole.DecorationRole and column == COLUMN_STATUS:
            return self._status_icon(job.status)
        if role == Qt.ItemDataRole.ForegroundRole and column == COLUMN_STATUS:
            # Resolved per paint, so a scheme change is correct on repaint.
            return status_color(job.status)
        if role == ROLE_PROGRESS and column == COLUMN_PROGRESS:
            return job.progress if job.status is JobStatus.RUNNING else None
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


class ProgressDelegate(QStyledItemDelegate):
    """Draws the progress column as a bar while a job is running.

    Painted through the active style rather than a stylesheet, so it follows
    the colour scheme for free.
    """

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        fraction = index.data(ROLE_PROGRESS)
        if not isinstance(fraction, float):
            # Not running, or running with no measurable progress: the text
            # the model supplies already says more than an empty bar would.
            super().paint(painter, option, index)
            return

        # Let the style paint selection and alternating-row background first.
        self.initStyleOption(option, index)
        option.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget
        )

        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(4, 4, -4, -4)
        bar.minimum = 0
        bar.maximum = _PROGRESS_STEPS
        bar.progress = round(fraction * _PROGRESS_STEPS)
        # A progress bar wraps its text rather than eliding it, and the stage
        # name is easily wider than the column, so elide it here.
        bar.text = option.fontMetrics.elidedText(
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
            Qt.TextElideMode.ElideRight,
            bar.rect.width() - _PROGRESS_TEXT_PADDING,
        )
        bar.textVisible = True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        bar.state = option.state
        style.drawControl(
            QStyle.ControlElement.CE_ProgressBar, bar, painter, option.widget
        )


class JobTableView(QTableView):
    def __init__(self, model: JobTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegateForColumn(COLUMN_PROGRESS, ProgressDelegate(self))
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        enable_row_stripes(self)
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

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self.model() is None or self.model().rowCount() != 0:
            return
        painter = QPainter(self.viewport())
        painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(
            self.viewport().rect().adjusted(24, 24, -24, -24),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            "No jobs yet\n"
            "Use Match folders to review a batch, or Add job for one pair.",
        )
        painter.end()

    def selected_rows(self) -> list[int]:
        selection = self.selectionModel()
        if selection is None:
            return []
        return sorted({index.row() for index in selection.selectedRows()})
