"""Completed-job history, with search and requeue."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ffsubsyncui.core.models import JobStatus, SyncJob
from ffsubsyncui.core.store import HistoryEntry, JobStore

_LABELS = {
    JobStatus.SUCCEEDED: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
    JobStatus.SKIPPED_LOW_QUALITY: "Low quality",
}


class HistoryDialog(QDialog):
    def __init__(
        self, store: JobStore, history_limit: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("History")
        self.resize(940, 560)
        self._store = store
        self._limit = max(1, history_limit or 500)
        self._entries: list[HistoryEntry] = []
        self._requeue: list[SyncJob] = []

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Filter by file name...")
        self.search.textChanged.connect(self._reload)
        clear = QPushButton("Clear history", self)
        clear.clicked.connect(self._clear)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        search_row.addWidget(clear)

        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(
            ("Finished", "Status", "Subtitle", "Offset", "Score", "Detail")
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 1, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

        self.summary = QLabel(self)
        requeue = QPushButton("Requeue selected", self)
        requeue.clicked.connect(self._accept_requeue)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.addButton(requeue, QDialogButtonBox.ButtonRole.ActionRole)

        layout = QVBoxLayout(self)
        layout.addLayout(search_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary)
        layout.addWidget(buttons)
        self._reload()

    @Slot()
    def _reload(self) -> None:
        self._entries = self._store.history(
            limit=self._limit, search=self.search.text()
        )
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            finished = datetime.fromtimestamp(entry.finished_at).strftime(
                "%Y-%m-%d %H:%M"
            )
            offset = (
                f"{entry.offset_seconds:+.3f}s"
                if entry.offset_seconds is not None
                else "-"
            )
            score = f"{entry.score:.1f}" if entry.score is not None else "-"
            detail = entry.error
            if not detail and entry.duration_seconds:
                detail = f"{entry.duration_seconds:.1f}s"
                if entry.used_speech_cache:
                    detail += ", reused cached speech"
            values = (
                finished,
                _LABELS.get(entry.status, entry.status.value),
                entry.subtitle_path.name,
                offset,
                score,
                detail,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 2:
                    item.setToolTip(
                        f"{entry.subtitle_path}\nreference: {entry.reference_path}"
                    )
                if column in (3, 4):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, item)
        self.summary.setText(f"{len(self._entries)} entries")

    @Slot()
    def _clear(self) -> None:
        confirmed = QMessageBox.question(
            self,
            "Clear history",
            "Delete every history entry? This cannot be undone.",
        )
        if confirmed is QMessageBox.StandardButton.Yes:
            self._store.clear_history()
            self._reload()

    @Slot()
    def _accept_requeue(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "Requeue", "Select one or more rows first.")
            return
        self._requeue = [self._entries[row].to_job() for row in rows]
        self.accept()

    def jobs(self) -> list[SyncJob]:
        return self._requeue
