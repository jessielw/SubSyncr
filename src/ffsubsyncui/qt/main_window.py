"""Main window: queue table, live log, and the toolbar that drives them."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import time

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QStyle,
    QToolBar,
    QWidget,
)

from ffsubsyncui.core.config import AppConfig, database_path, speech_cache_dir
from ffsubsyncui.core.ffmpeg import probe
from ffsubsyncui.core.models import JobStatus, SyncJob
from ffsubsyncui.core.queue import QueueCounts, SyncQueue
from ffsubsyncui.core.speech_cache import SpeechCache
from ffsubsyncui.core.store import JobStore
from ffsubsyncui.qt.add_job import AddJobDialog
from ffsubsyncui.qt.bridge import QueueBridge
from ffsubsyncui.qt.history_view import HistoryDialog
from ffsubsyncui.qt.match_view import BatchMatchDialog
from ffsubsyncui.qt.queue_view import COLUMN_PROGRESS, JobTableModel, JobTableView
from ffsubsyncui.qt.settings_view import SettingsDialog

_MAX_LOG_BLOCKS = 5000

# Don't guess at a rate until a couple of jobs have actually finished.
_MIN_SAMPLES_FOR_ETA = 2


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        store: JobStore | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or QSettings()
        self.config = config or AppConfig.load()
        self.store = store or JobStore(database_path())
        self.cache = SpeechCache(
            speech_cache_dir(), enabled=self.config.speech_cache_enabled
        )
        self.queue = SyncQueue(self.store, self.config, self.cache)
        self.bridge = QueueBridge(self.queue, self)

        self.setWindowTitle("FFSubSync UI")
        self.setMinimumSize(960, 600)
        self.resize(1240, 760)

        self.model = JobTableModel(self.queue, self)
        self.table = JobTableView(self.model, self)
        self.table.selectionModel().selectionChanged.connect(self._update_actions)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("ffsubsync output appears here while jobs run.")
        self.log.document().setMaximumBlockCount(_MAX_LOG_BLOCKS)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.queue_label = QLabel(self)
        self.ffmpeg_label = QLabel(self)
        self.statusBar().addWidget(self.queue_label, 1)
        self.statusBar().addPermanentWidget(self.ffmpeg_label)

        # Batch timing, for the estimate in the status bar.
        self._batch_started: float | None = None
        self._batch_baseline = 0

        self._build_toolbar()
        self._connect_bridge()
        self._restore_window_state()
        self._update_status()
        self._update_ffmpeg_status()
        self._update_actions()

    # -- construction ----------------------------------------------------

    def _action(
        self, label: str, icon: QStyle.StandardPixmap, callback: Callable[[], None]
    ) -> QAction:
        action = QAction(self.style().standardIcon(icon), label, self)
        action.triggered.connect(callback)
        return action

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Queue", self)
        toolbar.setObjectName("queueToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        pixmap = QStyle.StandardPixmap
        self.match_action = self._action(
            "Match folders", pixmap.SP_DirOpenIcon, self._match_folders
        )
        self.add_action = self._action(
            "Add job", pixmap.SP_FileDialogNewFolder, self._add_job
        )
        self.remove_action = self._action(
            "Remove", pixmap.SP_TrashIcon, self._remove_selected
        )
        self.retry_action = self._action(
            "Retry", pixmap.SP_BrowserReload, self._retry_selected
        )
        self.clear_action = self._action(
            "Clear finished", pixmap.SP_DialogDiscardButton, self._clear_finished
        )
        self.start_action = self._action(
            "Start queue", pixmap.SP_MediaPlay, self.queue.start
        )
        self.stop_action = self._action(
            "Stop after current", pixmap.SP_MediaPause, self.queue.stop
        )
        self.cancel_action = self._action(
            "Cancel running", pixmap.SP_MediaStop, self._cancel_selected
        )
        self.history_action = self._action(
            "History", pixmap.SP_FileDialogContentsView, self._show_history
        )
        self.settings_action = self._action(
            "Settings", pixmap.SP_FileDialogDetailedView, self._show_settings
        )

        for action in (self.match_action, self.add_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (self.remove_action, self.retry_action, self.clear_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (self.start_action, self.stop_action, self.cancel_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (self.history_action, self.settings_action):
            toolbar.addAction(action)

    def _connect_bridge(self) -> None:
        self.bridge.queue_changed.connect(self._queue_changed)
        self.bridge.job_changed.connect(self._job_changed)
        self.bridge.progress.connect(self._job_progress)
        self.bridge.log.connect(self._append_log)
        self.bridge.running_changed.connect(self._running_changed)

    # -- bridge slots ----------------------------------------------------

    @Slot()
    def _queue_changed(self) -> None:
        self.model.refresh()
        self._update_status()
        self._update_actions()

    @Slot(str)
    def _job_changed(self, job_id: str) -> None:
        self.model.touch(job_id)
        self._update_status()
        self._update_actions()

    @Slot(str, object, str)
    def _job_progress(self, job_id: str, fraction: object, stage: str) -> None:
        del fraction, stage  # The job object already holds the current values.
        self.model.touch(job_id, COLUMN_PROGRESS, COLUMN_PROGRESS)

    @Slot(str, str, str)
    def _append_log(self, job_id: str, level: str, message: str) -> None:
        job = self.queue.find(job_id)
        name = job.subtitle_path.name if job else job_id.split("-", 1)[0]
        self.log.appendPlainText(f"[{name}] {level}: {message}")

    @Slot(bool)
    def _running_changed(self, running: bool) -> None:
        if running and self._batch_started is None:
            self._batch_started = time.monotonic()
            self._batch_baseline = self.queue.counts().finished
        elif not running:
            self._batch_started = None
        self._update_status()
        self._update_actions()

    # -- toolbar slots ---------------------------------------------------

    @Slot()
    def _match_folders(self) -> None:
        dialog = BatchMatchDialog(
            self.config.sync,
            threshold=self.config.match_threshold,
            recursive=self.config.recursive_scan,
            fast_mode=self.config.fast_mode,
            output_suffix=self.config.output_suffix,
            video_folder=str(self.settings.value("match/videoFolder", "")),
            subtitle_folder=str(self.settings.value("match/subtitleFolder", "")),
            parent=self,
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        # Remember the folders either way, so a cancelled scan is not retyped.
        self.settings.setValue("match/videoFolder", dialog.video_folder.text())
        self.settings.setValue("match/subtitleFolder", dialog.subtitle_folder.text())
        self._remember_fast_mode(dialog.fast_mode.isChecked())
        if accepted:
            self._queue_jobs(dialog.jobs(), "Queued")

    @Slot()
    def _add_job(self) -> None:
        dialog = AddJobDialog(
            self.config.sync,
            output_suffix=self.config.output_suffix,
            fast_mode=self.config.fast_mode,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._remember_fast_mode(dialog.fast_mode.isChecked())
            self._queue_jobs([dialog.job()], "Queued")

    def _remember_fast_mode(self, enabled: bool) -> None:
        """Carry a per-batch choice into the next batch."""
        if enabled == self.config.fast_mode:
            return
        self.config.fast_mode = enabled
        with contextlib.suppress(OSError):
            self.config.save()

    def _queue_jobs(self, jobs: list[SyncJob], verb: str) -> None:
        added = self.queue.add(jobs)
        if not added:
            return
        groups = self.queue.pending_groups()
        detail = f"{verb} {added} jobs across {groups} videos"
        if self.config.auto_start:
            self.queue.start()
            detail += " - running"
        self.statusBar().showMessage(detail, 6000)

    def _selected_ids(self) -> list[str]:
        return [job.id for job in self.model.jobs_for_rows(self.table.selected_rows())]

    @Slot()
    def _remove_selected(self) -> None:
        removed = self.queue.remove(self._selected_ids())
        if not removed:
            self.statusBar().showMessage(
                "Running jobs must be cancelled before they can be removed", 5000
            )

    @Slot()
    def _retry_selected(self) -> None:
        self.queue.retry(self._selected_ids())

    @Slot()
    def _cancel_selected(self) -> None:
        jobs = self.model.jobs_for_rows(self.table.selected_rows())
        running = [job for job in jobs if job.status is JobStatus.RUNNING]
        # With nothing running selected, treat the button as "cancel everything".
        if not running:
            self.queue.cancel_all()
            return
        for job in running:
            self.queue.cancel(job.id)

    @Slot()
    def _clear_finished(self) -> None:
        self.queue.clear_finished()

    @Slot()
    def _show_history(self) -> None:
        dialog = HistoryDialog(self.store, self.config.history_limit, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._queue_jobs(dialog.jobs(), "Requeued")

    @Slot()
    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.config()
        # The queue holds a reference to this object, so mutate rather than
        # rebind; concurrency then takes effect on the next scheduling pass.
        self.config.sync = updated.sync
        self.config.concurrency = updated.concurrency
        self.config.match_threshold = updated.match_threshold
        self.config.recursive_scan = updated.recursive_scan
        self.config.output_suffix = updated.output_suffix
        self.config.speech_cache_enabled = updated.speech_cache_enabled
        self.config.history_limit = updated.history_limit
        self.config.auto_start = updated.auto_start
        self.cache.enabled = updated.speech_cache_enabled
        try:
            self.config.save()
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not save settings: {exc}")
        self._update_ffmpeg_status()

    # -- status ----------------------------------------------------------

    def _update_actions(self) -> None:
        running = self.queue.is_running
        counts = self.queue.counts()
        selected = self.model.jobs_for_rows(self.table.selected_rows())
        self.start_action.setEnabled(counts.ready > 0)
        self.stop_action.setEnabled(running)
        self.cancel_action.setEnabled(running)
        self.remove_action.setEnabled(bool(selected))
        self.retry_action.setEnabled(any(job.status.is_retryable for job in selected))
        self.clear_action.setEnabled(
            any(job.status.is_terminal for job in self.queue.snapshot())
        )

    def _update_status(self) -> None:
        counts = self.queue.counts()
        parts = [
            f"{counts.ready} ready",
            f"{counts.running} running",
            f"{counts.succeeded} done",
        ]
        if counts.failed:
            parts.append(f"{counts.failed} failed")
        if counts.skipped:
            parts.append(f"{counts.skipped} low quality")
        if counts.cancelled:
            parts.append(f"{counts.cancelled} cancelled")
        if remaining := self._estimate_remaining(counts):
            parts.append(remaining)
        self.queue_label.setText("   -   ".join(parts))

    def _estimate_remaining(self, counts: QueueCounts) -> str:
        """A rough time-left figure, from how fast this batch is actually going."""
        if self._batch_started is None:
            return ""
        completed = counts.finished - self._batch_baseline
        outstanding = counts.ready + counts.running
        if completed < _MIN_SAMPLES_FOR_ETA or not outstanding:
            return ""
        elapsed = time.monotonic() - self._batch_started
        seconds = (elapsed / completed) * outstanding
        if seconds < 90:
            return f"~{seconds:.0f}s left"
        return f"~{seconds / 60:.0f} min left"

    def _update_ffmpeg_status(self) -> None:
        status = probe(self.config.sync.ffmpeg_path)
        self.ffmpeg_label.setText(status.message)
        self.ffmpeg_label.setStyleSheet(
            "color: #15803d;" if status.ready else "color: #b45309;"
        )

    # -- lifecycle -------------------------------------------------------

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        state = self.settings.value("window/state")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        if isinstance(state, QByteArray):
            self.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.queue.is_running:
            answer = QMessageBox.question(
                self,
                "Sync in progress",
                "Jobs are still running. Cancel them and quit?",
            )
            if answer is not QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        # Stop callbacks before tearing down, so no signal reaches a dead widget.
        self.bridge.detach()
        self.queue.shutdown()
        self.store.prune_history(self.config.history_limit)
        self.store.close()
        super().closeEvent(event)
