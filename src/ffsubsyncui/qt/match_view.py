"""Scan two folders, review every proposed pairing, then queue them.

Review is the point of this dialog. Nothing is queued that the user has not
had a chance to see, correct, or exclude.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
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

from ffsubsyncui.core.language import parse_subtitle_tags
from ffsubsyncui.core.matcher import DEFAULT_THRESHOLD, propose_matches
from ffsubsyncui.core.models import MatchMethod, SyncJob, SyncOptions
from ffsubsyncui.core.planner import build_job, group_key_for
from ffsubsyncui.core.scanner import scan_references, scan_subtitles

SUBTITLE_FILTER = "Subtitle files (*.srt *.ass *.ssa *.sub *.vtt);;All files (*)"

_UNMATCHED_BRUSH = QBrush(QColor("#7c2d12"))
_AMBIGUOUS_BRUSH = QBrush(QColor("#78350f"))

COLUMN_INCLUDE = 0
COLUMN_VIDEO = 1
COLUMN_SUBTITLE = 2
COLUMN_LANGUAGE = 3
COLUMN_METHOD = 4
COLUMN_CONFIDENCE = 5
COLUMN_NOTES = 6


@dataclass
class _Row:
    video: Path
    subtitle: Path | None
    language: str | None
    method: MatchMethod
    confidence: float
    reasons: tuple[str, ...] = ()
    include: bool = True

    @property
    def ambiguous(self) -> bool:
        return any("runner-up" in reason for reason in self.reasons)


class PathField(QWidget):
    """A line edit with a folder browse button."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.edit = QLineEdit(self)
        button = QPushButton("Browse...", self)
        button.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(button)

    def text(self) -> str:
        return self.edit.text().strip()

    def set_text(self, value: str) -> None:
        self.edit.setText(value)

    @Slot()
    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select folder", self.text())
        if selected:
            self.set_text(selected)


class BatchMatchDialog(QDialog):
    def __init__(
        self,
        options: SyncOptions,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        recursive: bool = True,
        fast_mode: bool = False,
        output_suffix: str = ".synced",
        video_folder: str = "",
        subtitle_folder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Match folders")
        self.resize(1080, 640)
        self._options = options
        self._output_suffix = output_suffix
        self._rows: list[_Row] = []

        self.video_folder = PathField(self)
        self.video_folder.set_text(video_folder)
        self.subtitle_folder = PathField(self)
        self.subtitle_folder.set_text(subtitle_folder)
        self.recursive = QCheckBox("Include subfolders", self)
        self.recursive.setChecked(recursive)
        self.subtitle_reference = QCheckBox("References are subtitles, not video", self)
        self.subtitle_reference.setToolTip(
            "Align subtitles against a known-good subtitle instead of against "
            "audio. This needs no ffmpeg and takes about a tenth of a second "
            "per file."
        )
        self.fast_mode = QCheckBox(
            "Fast mode - sample the audio instead of reading the whole file", self
        )
        self.subtitle_reference.toggled.connect(
            lambda on: self.fast_mode.setEnabled(not on)
        )
        self.fast_mode.setChecked(fast_mode)
        self.fast_mode.setToolTip(
            "Extracts a handful of short segments spread across the reference "
            "rather than decoding it end to end. On a long film that is "
            "dramatically faster for the same answer; on a short episode there "
            "is little to save. Segment count and intro/outro skipping live in "
            "Settings, under Alignment."
        )
        self.threshold = QDoubleSpinBox(self)
        self.threshold.setRange(0.0, 100.0)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(threshold)
        self.threshold.setToolTip(
            "Fuzzy name similarity a pairing must reach before it is proposed."
        )
        scan_button = QPushButton("Scan", self)
        scan_button.setDefault(True)
        scan_button.clicked.connect(self._scan)

        form = QFormLayout()
        form.addRow("Video folder", self.video_folder)
        form.addRow("Subtitle folder", self.subtitle_folder)
        form.addRow("", self.recursive)
        form.addRow("", self.subtitle_reference)
        form.addRow("", self.fast_mode)
        form.addRow("Similarity threshold", self.threshold)
        source_box = QGroupBox("Source", self)
        source_layout = QVBoxLayout(source_box)
        source_layout.addLayout(form)
        scan_row = QHBoxLayout()
        scan_row.addStretch(1)
        scan_row.addWidget(scan_button)
        source_layout.addLayout(scan_row)

        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels(
            ("Queue", "Video", "Subtitle", "Language", "Method", "Match", "Notes")
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._item_changed)
        self.table.doubleClicked.connect(self._choose_for_current)
        header = self.table.horizontalHeader()
        for column in (
            COLUMN_INCLUDE,
            COLUMN_LANGUAGE,
            COLUMN_METHOD,
            COLUMN_CONFIDENCE,
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (COLUMN_VIDEO, COLUMN_SUBTITLE, COLUMN_NOTES):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

        choose = QPushButton("Choose subtitle...", self)
        choose.clicked.connect(self._choose_for_current)
        clear = QPushButton("Clear pairing", self)
        clear.clicked.connect(self._clear_selected)
        include_all = QPushButton("Queue all matched", self)
        include_all.clicked.connect(lambda: self._set_include_all(True))
        exclude_all = QPushButton("Queue none", self)
        exclude_all.clicked.connect(lambda: self._set_include_all(False))

        actions = QHBoxLayout()
        for button in (choose, clear, include_all, exclude_all):
            actions.addWidget(button)
        actions.addStretch(1)

        self.summary = QLabel("Choose folders and scan.", self)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText("Add to queue")
        self._ok_button.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(source_box)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(self.summary)
        layout.addWidget(buttons)

    # -- scanning --------------------------------------------------------

    @Slot()
    def _scan(self) -> None:
        video_dir = Path(self.video_folder.text()) if self.video_folder.text() else None
        subtitle_text = self.subtitle_folder.text()
        if video_dir is None or not video_dir.is_dir():
            QMessageBox.warning(self, "Scan", "Choose a video folder first.")
            return
        # An empty subtitle folder means "same place as the video files", which
        # is the common layout.
        subtitle_dir = Path(subtitle_text) if subtitle_text else video_dir
        if not subtitle_dir.is_dir():
            QMessageBox.warning(self, "Scan", "The subtitle folder does not exist.")
            return

        recursive = self.recursive.isChecked()
        subtitle_reference = self.subtitle_reference.isChecked()
        videos = scan_references(
            video_dir, subtitles_as_reference=subtitle_reference, recursive=recursive
        )
        subtitles = scan_subtitles(subtitle_dir, recursive=recursive)
        if not videos:
            kind = "subtitle" if subtitle_reference else "video"
            QMessageBox.information(self, "Scan", f"No {kind} references found.")
            return
        self.load_files(videos, subtitles)

    def load_files(self, references: list[Path], subtitles: list[Path]) -> None:
        """Populate the review grid from an explicit set of files."""
        # A file cannot be its own reference, which is easy to hit when the
        # references are subtitles from the same folder.
        chosen = set(references)
        subtitles = [path for path in subtitles if path not in chosen]

        plan = propose_matches(references, subtitles, threshold=self.threshold.value())
        self._rows = [
            _Row(
                video=match.video,
                subtitle=match.subtitle,
                language=match.language,
                method=match.method,
                confidence=match.confidence,
                reasons=match.reasons,
                include=match.subtitle is not None,
            )
            for match in plan.matches
        ]
        self._populate()
        leftover = len(plan.unmatched_subtitles)
        self._extra_note = f", {leftover} subtitles matched nothing" if leftover else ""
        self._update_summary()

    _extra_note = ""

    def _populate(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(
                Qt.CheckState.Checked if row.include else Qt.CheckState.Unchecked
            )
            self.table.setItem(index, COLUMN_INCLUDE, check)
            self._fill_row(index, row)
        self.table.blockSignals(False)

    def _fill_row(self, index: int, row: _Row) -> None:
        cells = {
            COLUMN_VIDEO: (row.video.name, str(row.video)),
            COLUMN_SUBTITLE: (
                row.subtitle.name if row.subtitle else "(no match)",
                str(row.subtitle) if row.subtitle else "",
            ),
            COLUMN_LANGUAGE: (row.language or "", ""),
            COLUMN_METHOD: (row.method.value, ""),
            COLUMN_CONFIDENCE: (
                f"{row.confidence:.0f}%" if row.subtitle else "-",
                "",
            ),
            COLUMN_NOTES: ("; ".join(row.reasons), "\n".join(row.reasons)),
        }
        for column, (text, tooltip) in cells.items():
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if tooltip:
                item.setToolTip(tooltip)
            if row.subtitle is None:
                item.setForeground(_UNMATCHED_BRUSH)
            elif row.ambiguous:
                item.setForeground(_AMBIGUOUS_BRUSH)
            self.table.setItem(index, column, item)

    # -- editing ---------------------------------------------------------

    @Slot()
    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != COLUMN_INCLUDE:
            return
        row = self._rows[item.row()]
        row.include = item.checkState() is Qt.CheckState.Checked
        self._update_summary()

    def _current_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectedIndexes()})

    @Slot()
    def _choose_for_current(self) -> None:
        rows = self._current_rows()
        if len(rows) != 1:
            QMessageBox.information(
                self, "Choose subtitle", "Select exactly one row first."
            )
            return
        index = rows[0]
        row = self._rows[index]
        start = str(row.subtitle.parent if row.subtitle else row.video.parent)
        selected, _ = QFileDialog.getOpenFileName(
            self, "Choose subtitle", start, SUBTITLE_FILTER
        )
        if not selected:
            return
        chosen = Path(selected)
        row.subtitle = chosen
        row.language = parse_subtitle_tags(chosen).label or None
        row.method = MatchMethod.MANUAL
        row.confidence = 100.0
        row.reasons = ("chosen by hand",)
        row.include = True
        self._refresh_row(index)
        self._update_summary()

    @Slot()
    def _clear_selected(self) -> None:
        for index in self._current_rows():
            row = self._rows[index]
            row.subtitle = None
            row.language = None
            row.method = MatchMethod.UNMATCHED
            row.confidence = 0.0
            row.reasons = ("cleared by hand",)
            row.include = False
            self._refresh_row(index)
        self._update_summary()

    def _refresh_row(self, index: int) -> None:
        self.table.blockSignals(True)
        row = self._rows[index]
        check = self.table.item(index, COLUMN_INCLUDE)
        if check is not None:
            check.setCheckState(
                Qt.CheckState.Checked if row.include else Qt.CheckState.Unchecked
            )
        self._fill_row(index, row)
        self.table.blockSignals(False)

    def _set_include_all(self, include: bool) -> None:
        self.table.blockSignals(True)
        for index, row in enumerate(self._rows):
            row.include = include and row.subtitle is not None
            check = self.table.item(index, COLUMN_INCLUDE)
            if check is not None:
                check.setCheckState(
                    Qt.CheckState.Checked if row.include else Qt.CheckState.Unchecked
                )
        self.table.blockSignals(False)
        self._update_summary()

    def _update_summary(self) -> None:
        queued = sum(1 for row in self._rows if row.include and row.subtitle)
        unmatched = sum(1 for row in self._rows if row.subtitle is None)
        ambiguous = sum(
            1 for row in self._rows if row.subtitle is not None and row.ambiguous
        )
        parts = [f"{queued} of {len(self._rows)} pairings will be queued"]
        if unmatched:
            parts.append(f"{unmatched} videos unmatched")
        if ambiguous:
            parts.append(f"{ambiguous} need a second look")
        self.summary.setText("  -  ".join(parts) + self._extra_note)
        self._ok_button.setEnabled(queued > 0)

    # -- result ----------------------------------------------------------

    def jobs(self) -> list[SyncJob]:
        # Fast mode is a per-batch decision, so it is applied here rather than
        # written back into the saved settings.
        fast = self.fast_mode.isChecked() and not self.subtitle_reference.isChecked()
        options = (
            replace(self._options, multi_segment_sync=True) if fast else self._options
        )
        keys: dict[Path, str] = {}
        jobs: list[SyncJob] = []
        for row in self._rows:
            if not row.include or row.subtitle is None:
                continue
            key = keys.setdefault(row.video, group_key_for(row.video))
            jobs.append(
                build_job(
                    row.video,
                    row.subtitle,
                    options,
                    output_suffix=self._output_suffix,
                    language=row.language,
                    group_key=key,
                )
            )
        return jobs
