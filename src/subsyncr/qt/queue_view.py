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

from subsyncr.core import engines, reference
from subsyncr.core.engines import EngineId
from subsyncr.core.models import JobStatus, SyncJob
from subsyncr.core.queue import SyncQueue
from subsyncr.core.scanner import needs_ffmpeg
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
COLUMN_TRACK = 3
COLUMN_SUBTITLE = 4
COLUMN_LANGUAGE = 5
COLUMN_ENGINE = 6
COLUMN_PROGRESS = 7
COLUMN_RESULT = 8


def track_text(job: SyncJob, resolving: bool | None = None) -> str:
    """What the job aligns against, in a few words.

    ``resolving`` says whether the queue is still reading the video; a video
    it could not read is left for the worker, and must not read "Probing...".
    """
    if not needs_ffmpeg(job.reference_path):
        return "File"  # The reference is already a subtitle or speech array.
    engine = job.options.engine
    if engine is EngineId.LAPSE:
        return "lapse decides"
    if job.reference_track is None:
        stream = job.options.ffsubsync.pgs_ref_stream.strip()
        if stream and stream != "auto" and not job.options.extract_embedded:
            return f"PGS · {stream}"  # A specifier passed through untouched.
        if resolving is None:
            resolving = job.status is JobStatus.QUEUED
        return "Probing..." if resolving else "Automatic"
    return reference.effective(job.reference_track, engine).describe()


def _track_tooltip(job: SyncJob, resolving: bool) -> str:
    if not needs_ffmpeg(job.reference_path):
        return "The reference is a subtitle or speech file, so it is used as is."
    if job.options.engine is EngineId.LAPSE:
        return "lapse searches the video's subtitle tracks itself."
    track = job.reference_track
    if track is None:
        if resolving:
            return "The video's tracks have not been read yet."
        return "The track is picked when the job runs."
    lines: list[str] = []
    if track.index is not None:
        lines.append(f"Stream s:{track.index}")
    shown = reference.effective(track, job.options.engine)
    if shown != track:
        lines.append(f"{track.describe()} needs ffsubsync; this engine uses the audio")
    lines.append("Chosen by you" if track.chosen else "Picked automatically")
    lines.append("Right-click to change it.")
    return "\n".join(lines)


def _engine_for(job: SyncJob) -> EngineId:
    """The engine that produced the result, else the one the job will start with."""
    if job.result is not None and job.result.engine is not None:
        return job.result.engine
    return job.options.engine


class JobTableModel(QAbstractTableModel):
    """A read-mostly mirror of ``SyncQueue``; the queue stays the source of truth."""

    HEADERS = (
        "Run",
        "Status",
        "Reference",
        "Track",
        "Subtitle",
        "Language",
        "Engine",
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
        jobs = self._queue.snapshot()
        if [job.id for job in jobs] == [job.id for job in self._jobs]:
            # Same rows (a group finishing, say): repaint rather than reset,
            # which would throw away the selection and scroll position.
            self._jobs = jobs
            if jobs:
                self.dataChanged.emit(
                    self.index(0, 0), self.index(len(jobs) - 1, self.columnCount() - 1)
                )
            return
        self.beginResetModel()
        self._jobs = jobs
        self._rows = {job.id: i for i, job in enumerate(self._jobs)}
        self.endResetModel()

    def touch(self, job_id: str, first: int = 0, last: int | None = None) -> None:
        """Repaint one row whose underlying job mutated in place."""
        self.touch_many([job_id], first, last)

    def touch_many(
        self, job_ids: list[str], first: int = 0, last: int | None = None
    ) -> None:
        """Repaint these rows with a single change notification.

        One notification for the span rather than one per row: each makes the
        view re-measure its auto-sized columns, and that is the expensive part.
        """
        rows = [
            row
            for row in (self._rows.get(job_id) for job_id in job_ids)
            if row is not None and row < len(self._jobs)
        ]
        if not rows:
            return
        end = self.columnCount() - 1 if last is None else last
        self.dataChanged.emit(self.index(min(rows), first), self.index(max(rows), end))

    def job_at(self, row: int) -> SyncJob | None:
        return self._jobs[row] if 0 <= row < len(self._jobs) else None

    def job_for_id(self, job_id: str) -> SyncJob | None:
        """The job shown for this id, without asking the queue (and its lock)."""
        row = self._rows.get(job_id)
        return self._jobs[row] if row is not None and row < len(self._jobs) else None

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
        if column == COLUMN_TRACK and role in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            resolving = self._queue.resolving(job)
            if role == Qt.ItemDataRole.DisplayRole:
                return track_text(job, resolving)
            return _track_tooltip(job, resolving)
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
            case _ if column == COLUMN_ENGINE:
                return engines.get(_engine_for(job)).label
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
            case _ if column == COLUMN_ENGINE:
                ran = _engine_for(job)
                if ran is not job.options.engine:
                    configured = engines.get(job.options.engine).label
                    return f"fell back from {configured}"
                return ""
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
                if _another_track_might_help(job):
                    parts.append("Right-click → Sync against to try another track.")
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


def _another_track_might_help(job: SyncJob) -> bool:
    """A failed alignment against a video, where the menu offers other tracks."""
    return (
        job.status in (JobStatus.FAILED, JobStatus.SKIPPED_LOW_QUALITY)
        and job.result is not None
        and needs_ffmpeg(job.reference_path)
        and job.options.engine is not EngineId.LAPSE
    )


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
            COLUMN_TRACK,
            COLUMN_LANGUAGE,
            COLUMN_ENGINE,
            COLUMN_PROGRESS,
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (COLUMN_REFERENCE, COLUMN_SUBTITLE, COLUMN_RESULT):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        # Size those columns from the rows on screen only. Qt's default reads
        # up to 1000 rows on every change notification, synchronously - on a
        # long queue each progress tick cost ~140 ms, which is what froze the
        # window mid-batch. These columns hold short labels, so the visible
        # rows are a fair sample.
        header.setResizeContentsPrecision(0)

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
