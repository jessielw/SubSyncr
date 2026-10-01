"""Main window: queue table, live log, and the toolbar that drives them."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from pathlib import Path
import time

from PySide6.QtCore import QByteArray, QPoint, QSettings, QSize, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QToolBar,
    QWidget,
)

from subsyncr import __version__
from subsyncr.core import engines, logger
from subsyncr.core.config import (
    AppConfig,
    cache_dir,
    config_path,
    data_dir,
    database_path,
    speech_cache_dir,
)
from subsyncr.core.engines import EngineId
from subsyncr.core.models import JobStatus, SyncJob
from subsyncr.core.queue import QueueCounts, SyncQueue
from subsyncr.core.scanner import MEDIA_EXTENSIONS, SUBTITLE_EXTENSIONS
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.core.store import JobStore
from subsyncr.qt.add_job import AddJobDialog
from subsyncr.qt.bridge import QueueBridge
from subsyncr.qt.history_view import HistoryDialog
from subsyncr.qt.icons import Icon
from subsyncr.qt.match_view import BatchMatchDialog
from subsyncr.qt.queue_view import COLUMN_PROGRESS, JobTableModel, JobTableView
from subsyncr.qt.settings_view import SettingsDialog
from subsyncr.qt.sizing import (
    ICON_SIZE,
    SIZE_MAIN_MINIMUM,
    SIZE_MAIN_WINDOW,
)
from subsyncr.qt.splitter import GripSplitter
from subsyncr.qt.theme import ThemeManager, apply_theme
from subsyncr.qt.widgets import FFmpegStatusLabel

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

        self.setWindowTitle("SubSyncr")
        self.setAcceptDrops(True)
        self.setMinimumSize(*SIZE_MAIN_MINIMUM)
        self.resize(*SIZE_MAIN_WINDOW)
        self.theme = ThemeManager.for_app()

        self.model = JobTableModel(self.queue, self)
        self.table = JobTableView(self.model, self)
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Engine output appears here while jobs run.")
        self.log.document().setMaximumBlockCount(_MAX_LOG_BLOCKS)

        self.splitter = GripSplitter(Qt.Orientation.Vertical, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.log)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.splitter)

        self.queue_label = QLabel(self)
        # One line: the status bar has no room to wrap a long ffmpeg path.
        self.ffmpeg_label = FFmpegStatusLabel(self, wrap=False)
        self.statusBar().addWidget(self.queue_label, 1)
        self.statusBar().addPermanentWidget(self.ffmpeg_label)
        self.statusBar().setSizeGripEnabled(True)

        # Batch timing, for the estimate in the status bar.
        self._batch_started: float | None = None
        self._batch_baseline = 0

        self._build_toolbar()
        self._build_menus()
        self._connect_bridge()
        self._restore_window_state()
        self._update_status()
        self._update_ffmpeg_status()
        self._update_actions()

    # -- construction ----------------------------------------------------

    def _action(
        self,
        label: str,
        icon: Icon,
        callback: Callable[[], object],
        *,
        tip: str,
        shortcut: QKeySequence.StandardKey | str | None = None,
    ) -> QAction:
        action = QAction(label, self)
        action.triggered.connect(callback)
        action.setToolTip(tip)
        # Shown in the status bar on hover, so the toolbar itself stays terse.
        action.setStatusTip(tip)
        if shortcut is not None:
            action.setShortcut(shortcut)
        if self.theme is not None:
            self.theme.register(action, icon)
        return action

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Queue", self)
        toolbar.setObjectName("queueToolbar")
        toolbar.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.match_action = self._action(
            "Match folders",
            Icon.MATCH,
            self._match_folders,
            tip="Scan a video folder and a subtitle folder, and review the pairings",
            shortcut=QKeySequence.StandardKey.Open,
        )
        self.add_action = self._action(
            "Add job",
            Icon.ADD,
            self._add_job,
            tip="Queue a single reference and subtitle pair",
            shortcut=QKeySequence.StandardKey.New,
        )
        self.remove_action = self._action(
            "Remove",
            Icon.REMOVE,
            self._remove_selected,
            tip="Remove the selected jobs from the queue",
            shortcut=QKeySequence.StandardKey.Delete,
        )
        self.retry_action = self._action(
            "Retry",
            Icon.RETRY,
            self._retry_selected,
            tip="Queue the selected failed, cancelled or low-quality jobs again",
            shortcut="Ctrl+R",
        )
        self.clear_action = self._action(
            "Clear finished",
            Icon.CLEAR,
            self._clear_finished,
            tip="Remove every job that has finished, however it ended",
        )
        self.start_action = self._action(
            "Start queue",
            Icon.START,
            self.queue.start,
            tip="Start working through the queued jobs",
            shortcut="Ctrl+Return",
        )
        self.stop_action = self._action(
            "Stop after current",
            Icon.STOP,
            self.queue.stop,
            tip="Let the running jobs finish, then stop",
            shortcut="Ctrl+.",
        )
        self.cancel_action = self._action(
            "Cancel running",
            Icon.CANCEL,
            self._cancel_selected,
            tip="Cancel the selected running jobs, or all of them if none is selected",
        )
        self.history_action = self._action(
            "History",
            Icon.HISTORY,
            self._show_history,
            tip="Search finished jobs and queue them again",
            shortcut="Ctrl+H",
        )
        self.settings_action = self._action(
            "Settings",
            Icon.SETTINGS,
            self._show_settings,
            tip="Engines, concurrency, matching and file handling",
            shortcut=QKeySequence.StandardKey.Preferences,
        )
        self.about_action = self._action(
            "About SubSyncr",
            Icon.ABOUT,
            self._show_about,
            tip="Version, engines and where things are stored",
        )
        self.docs_action = self._action(
            "Documentation",
            Icon.ABOUT,
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/jessielw/SubSyncr#readme")
            ),
            tip="Read setup instructions and the first-sync guide",
        )
        self.report_action = self._action(
            "Report a problem",
            Icon.OPEN_FOLDER,
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/jessielw/SubSyncr/issues/new/choose")
            ),
            tip="Open the issue form; no diagnostics are sent automatically",
        )
        self.diagnostics_action = self._action(
            "Copy diagnostics",
            Icon.ABOUT,
            self._copy_diagnostics,
            tip="Copy versions and platform details without media paths or history",
        )
        self.quit_action = self._action(
            "Quit",
            Icon.QUIT,
            self.close,
            tip="Close SubSyncr",
            shortcut=QKeySequence.StandardKey.Quit,
        )
        self.open_folder_action = self._action(
            "Open containing folder",
            Icon.OPEN_FOLDER,
            self._open_containing_folder,
            tip="Show the selected job's output folder in the file manager",
        )

        for group in (
            (self.match_action, self.add_action),
            (self.start_action, self.stop_action, self.cancel_action),
            (self.settings_action, self.history_action),
        ):
            if toolbar.actions():
                toolbar.addSeparator()
            for action in group:
                toolbar.addAction(action)

    def _build_menus(self) -> None:
        """The same action objects again, so enabled state stays in one place."""
        menus: tuple[tuple[str, tuple[QAction | None, ...]], ...] = (
            ("&File", (self.match_action, self.add_action, None, self.quit_action)),
            (
                "&Queue",
                (
                    self.start_action,
                    self.stop_action,
                    self.cancel_action,
                    None,
                    self.retry_action,
                    self.remove_action,
                    self.clear_action,
                ),
            ),
            ("&Tools", (self.history_action, self.settings_action)),
            (
                "&Help",
                (
                    self.docs_action,
                    self.report_action,
                    self.diagnostics_action,
                    None,
                    self.about_action,
                ),
            ),
        )
        bar = self.menuBar()
        for title, actions in menus:
            menu = bar.addMenu(title)
            for action in actions:
                if action is None:
                    menu.addSeparator()
                else:
                    menu.addAction(action)

    @Slot(QPoint)
    def _show_table_menu(self, position: QPoint) -> None:
        menu = QMenu(self.table)
        for action in (
            self.start_action,
            self.cancel_action,
            None,
            self.retry_action,
            self.remove_action,
            None,
            self.open_folder_action,
        ):
            if action is None:
                menu.addSeparator()
            else:
                menu.addAction(action)
        menu.exec(self.table.viewport().mapToGlobal(position))

    @Slot()
    def _open_containing_folder(self) -> None:
        jobs = self.model.jobs_for_rows(self.table.selected_rows())
        if not jobs:
            return
        # The output is what there is to look at once a job is done, and it
        # sits beside the input before then.
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(jobs[0].effective_output.parent))
        )

    @Slot()
    def _copy_diagnostics(self) -> None:
        from subsyncr.core.diagnostics import support_summary

        QApplication.clipboard().setText(support_summary())
        self.statusBar().showMessage(
            "Diagnostics copied (no media paths or history)", 5000
        )

    @Slot()
    def _show_about(self) -> None:
        names = ", ".join(spec.label for spec in engines.all_specs())
        QMessageBox.about(
            self,
            "About SubSyncr",
            f"<h3>SubSyncr {__version__}</h3>"
            "<p>A review-first batch queue for subtitle synchronization.</p>"
            f"<p><b>Engines</b><br>{names}</p>"
            "<p><b>Where things are stored</b><br>"
            f"Settings: {config_path()}<br>"
            f"Queue and history: {database_path()}<br>"
            f"Data: {data_dir()}<br>"
            f"Cache: {cache_dir()}</p>",
        )

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

    # -- drag and drop ---------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if not paths:
            return
        event.acceptProposedAction()
        # On Windows the drop arrives inside the OS drag-and-drop loop
        # (OLE's DoDragDrop), which blocks Explorer's UI thread until this
        # handler returns. Opening a modal dialog here nests a second event
        # loop inside that call and leaves Explorer stuck until the dialog
        # closes, so defer the actual handling to the next event loop tick,
        # once control has returned to the drag source.
        QTimer.singleShot(0, lambda: self._handle_dropped_paths(paths))

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        folders = [path for path in paths if path.is_dir()]
        files = [path for path in paths if path.is_file()]
        if folders:
            self._match_folders(
                video_folder=str(folders[0]),
                subtitle_folder=str(folders[1]) if len(folders) > 1 else "",
            )
        elif files:
            self._match_dropped_files(files)

    def _match_dropped_files(self, files: list[Path]) -> None:
        """Review an explicit set of dropped files rather than a folder scan."""
        media = [path for path in files if path.suffix.casefold() in MEDIA_EXTENSIONS]
        subtitles = [
            path for path in files if path.suffix.casefold() in SUBTITLE_EXTENSIONS
        ]
        if len(media) == 1 and not subtitles:
            # Exactly one half of a pair: ask for the other half directly,
            # rather than a dead-end error.
            self._run_add_job_dialog(self._build_add_job_dialog(reference=media[0]))
            return
        if not media and len(subtitles) == 1:
            self._run_add_job_dialog(self._build_add_job_dialog(subtitle=subtitles[0]))
            return
        if not media:
            # A flat pile of 2+ subtitles cannot say which of them is the
            # known-good one, so there is nothing to guess at here.
            QMessageBox.information(
                self,
                "Drop files",
                "Drop the video files as well. To align subtitles against a "
                "reference subtitle, use Match folders and tick "
                "'References are subtitles, not video'.",
            )
            return
        dialog = self._build_match_dialog(
            video_folder=str(media[0].parent),
            subtitle_folder=(
                str(subtitles[0].parent) if subtitles else str(media[0].parent)
            ),
        )
        dialog.load_files(media, subtitles)
        self._run_match_dialog(dialog)

    # -- toolbar slots ---------------------------------------------------

    def _build_match_dialog(
        self, *, video_folder: str = "", subtitle_folder: str = ""
    ) -> BatchMatchDialog:
        return BatchMatchDialog(
            self.config.sync,
            threshold=self.config.match_threshold,
            recursive=self.config.recursive_scan,
            fast_mode=self.config.fast_mode,
            output_suffix=self.config.output_suffix,
            video_folder=video_folder
            or str(self.settings.value("match/videoFolder", "")),
            subtitle_folder=subtitle_folder
            or str(self.settings.value("match/subtitleFolder", "")),
            parent=self,
        )

    def _run_match_dialog(self, dialog: BatchMatchDialog) -> None:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        # Remember the folders either way, so a cancelled scan is not retyped.
        self.settings.setValue("match/videoFolder", dialog.video_folder.text())
        self.settings.setValue("match/subtitleFolder", dialog.subtitle_folder.text())
        self._remember_batch_choices(
            dialog.fast_mode.isChecked(), dialog.engine.engine()
        )
        if accepted:
            self._queue_jobs(dialog.jobs(), "Queued")

    @Slot()
    def _match_folders(
        self, *, video_folder: str = "", subtitle_folder: str = ""
    ) -> None:
        dialog = self._build_match_dialog(
            video_folder=video_folder, subtitle_folder=subtitle_folder
        )
        if video_folder:
            dialog.scan()
        self._run_match_dialog(dialog)

    def _build_add_job_dialog(
        self, *, reference: Path | None = None, subtitle: Path | None = None
    ) -> AddJobDialog:
        dialog = AddJobDialog(
            self.config.sync,
            output_suffix=self.config.output_suffix,
            fast_mode=self.config.fast_mode,
            parent=self,
        )
        if reference is not None:
            dialog.reference.set_text(str(reference))
        if subtitle is not None:
            dialog.subtitle.set_text(str(subtitle))
        # Put the cursor on whichever field still needs filling in.
        if reference is not None and subtitle is None:
            dialog.subtitle.edit.setFocus()
        elif subtitle is not None and reference is None:
            dialog.reference.edit.setFocus()
        return dialog

    def _run_add_job_dialog(self, dialog: AddJobDialog) -> None:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._remember_batch_choices(
                dialog.fast_mode.isChecked(), dialog.engine.engine()
            )
            self._queue_jobs([dialog.job()], "Queued")

    @Slot()
    def _add_job(self) -> None:
        self._run_add_job_dialog(self._build_add_job_dialog())

    def _remember_batch_choices(self, fast_mode: bool, engine: EngineId) -> None:
        """Carry per-batch choices into the next batch."""
        if fast_mode == self.config.fast_mode and engine is self.config.sync.engine:
            return
        self.config.fast_mode = fast_mode
        # The dialog's pick becomes the default, so Settings shows it too.
        self.config.sync.engine = engine
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
        self.config.theme = updated.theme
        apply_theme(self.config.theme)
        self.config.concurrency = updated.concurrency
        self.config.match_threshold = updated.match_threshold
        self.config.recursive_scan = updated.recursive_scan
        self.config.output_suffix = updated.output_suffix
        self.config.speech_cache_enabled = updated.speech_cache_enabled
        self.config.history_limit = updated.history_limit
        self.config.auto_start = updated.auto_start
        self.config.log_level = updated.log_level
        self.config.log_retention_days = updated.log_retention_days
        logger.set_level(updated.log_level)
        logger.set_retention(updated.log_retention_days)
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
        self.open_folder_action.setEnabled(bool(selected))

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
        self.ffmpeg_label.refresh(self.config.sync.environment.ffmpeg_path)

    # -- lifecycle -------------------------------------------------------

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        state = self.settings.value("window/state")
        splitter = self.settings.value("window/splitter")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        if isinstance(state, QByteArray):
            self.restoreState(state)
        # QMainWindow.saveState covers toolbars and docks, not the central
        # widget, so the log pane would otherwise reset on every launch.
        if isinstance(splitter, QByteArray):
            self.splitter.restoreState(splitter)

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
        self.settings.setValue("window/splitter", self.splitter.saveState())
        # Stop callbacks before tearing down, so no signal reaches a dead widget.
        self.bridge.detach()
        self.queue.shutdown()
        self.store.prune_history(self.config.history_limit)
        self.store.close()
        super().closeEvent(event)
