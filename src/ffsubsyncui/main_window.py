from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QStyle,
    QTableView,
    QToolBar,
    QWidget,
)

from ffsubsyncui.dialogs import AddJobDialog, BatchMatchDialog, SettingsDialog
from ffsubsyncui.domain import JobStatus
from ffsubsyncui.job_model import JobTableModel
from ffsubsyncui.persistence import QueueStore
from ffsubsyncui.queue_controller import QueueController
from ffsubsyncui.runner import ffmpeg_status
from ffsubsyncui.settings import load_default_options, save_default_options


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        queue_store: QueueStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or QSettings()
        self.queue_store = queue_store or QueueStore()
        self.default_options = load_default_options(self.settings)
        self.setWindowTitle("FFSubSync UI")
        self.setMinimumSize(900, 560)
        self.resize(1180, 720)

        self.model = JobTableModel(self.queue_store.load(), self)
        self.controller = QueueController(self.model, self)
        self.model.persistence_changed.connect(self._save_queue)
        self.model.persistence_changed.connect(self._update_status)
        self.controller.running_changed.connect(self._running_changed)
        self.controller.job_log.connect(self._append_job_log)

        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        self.log = QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Select and run jobs to see ffsubsync output here.")
        self.log.document().setMaximumBlockCount(3000)

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
        self._create_toolbar()
        self._restore_window_state()
        self._update_status()
        self._update_ffmpeg_status()

    def _create_toolbar(self) -> None:
        toolbar = QToolBar("Queue", self)
        toolbar.setObjectName("queueToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.add_action = self._action(
            "Add job",
            QStyle.StandardPixmap.SP_FileDialogNewFolder,
            self._add_job,
        )
        self.match_action = self._action(
            "Match folders",
            QStyle.StandardPixmap.SP_DirOpenIcon,
            self._batch_match,
        )
        self.remove_action = self._action(
            "Remove",
            QStyle.StandardPixmap.SP_TrashIcon,
            self._remove_selected,
        )
        self.retry_action = self._action(
            "Retry",
            QStyle.StandardPixmap.SP_BrowserReload,
            self._retry_selected,
        )
        self.clear_action = self._action(
            "Clear completed",
            QStyle.StandardPixmap.SP_DialogDiscardButton,
            self.model.clear_completed,
        )
        self.start_action = self._action(
            "Start queue",
            QStyle.StandardPixmap.SP_MediaPlay,
            self.controller.start,
        )
        self.stop_action = self._action(
            "Stop after current",
            QStyle.StandardPixmap.SP_MediaStop,
            self.controller.stop_after_current,
        )
        self.settings_action = self._action(
            "Settings",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            self._show_settings,
        )
        self.stop_action.setEnabled(False)

        for action in (
            self.add_action,
            self.match_action,
            self.remove_action,
            self.retry_action,
            self.clear_action,
        ):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.start_action)
        toolbar.addAction(self.stop_action)
        toolbar.addSeparator()
        toolbar.addAction(self.settings_action)

    def _action(
        self,
        label: str,
        icon: QStyle.StandardPixmap,
        callback: Callable[[], None],
    ) -> QAction:
        action = QAction(self.style().standardIcon(icon), label, self)
        action.triggered.connect(callback)
        return action

    @Slot()
    def _add_job(self) -> None:
        dialog = AddJobDialog(self.default_options, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.model.add_jobs((dialog.job(),))

    @Slot()
    def _batch_match(self) -> None:
        dialog = BatchMatchDialog(self.default_options, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.model.add_jobs(dialog.jobs())

    def _selected_rows(self) -> list[int]:
        return sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()}
        )

    @Slot()
    def _remove_selected(self) -> None:
        self.model.remove_rows(self._selected_rows())

    @Slot()
    def _retry_selected(self) -> None:
        self.model.retry_rows(self._selected_rows())

    @Slot()
    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.default_options, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.default_options = dialog.options()
            save_default_options(self.settings, self.default_options)
            self._update_ffmpeg_status()

    @Slot(bool)
    def _running_changed(self, running: bool) -> None:
        self.start_action.setEnabled(not running)
        self.stop_action.setEnabled(running)
        self._update_status()

    @Slot(str, str)
    def _append_job_log(self, job_id: str, message: str) -> None:
        short_id = job_id.split("-", 1)[0]
        self.log.appendPlainText(f"[{short_id}] {message}")

    @Slot()
    def _save_queue(self) -> None:
        try:
            self.queue_store.save(self.model.jobs)
        except OSError as exc:
            self.statusBar().showMessage(f"Could not save queue: {exc}", 8000)

    @Slot()
    def _update_status(self) -> None:
        counts = self.model.counts()
        enabled = sum(
            job.enabled and job.status is JobStatus.QUEUED for job in self.model.jobs
        )
        self.queue_label.setText(
            f"{enabled} ready  •  {counts[JobStatus.RUNNING]} running  •  "
            f"{counts[JobStatus.SUCCEEDED]} complete  •  "
            f"{counts[JobStatus.FAILED]} failed"
        )

    def _update_ffmpeg_status(self) -> None:
        ready, message = ffmpeg_status(self.default_options.ffmpeg_path)
        self.ffmpeg_label.setText(message)
        self.ffmpeg_label.setStyleSheet(
            "color: #15803d;" if ready else "color: #b45309;"
        )

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        state = self.settings.value("window/state")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        if isinstance(state, QByteArray):
            self.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.controller.is_running:
            QMessageBox.information(
                self,
                "Sync in progress",
                "The active sync must finish before the application can close. "
                "Use 'Stop after current' to prevent another queued job from starting.",
            )
            event.ignore()
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        self._save_queue()
        super().closeEvent(event)
