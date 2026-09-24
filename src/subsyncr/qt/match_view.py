"""Scan two folders, review every proposed pairing, then queue them.

Review is the point of this dialog. Nothing is queued that the user has not
had a chance to see, correct, or exclude.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Slot
from PySide6.QtGui import QBrush
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
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from subsyncr.core import engines
from subsyncr.core.engine import capability_problem, first_capable
from subsyncr.core.engines import EngineId
from subsyncr.core.language import parse_subtitle_tags
from subsyncr.core.matcher import DEFAULT_THRESHOLD, propose_matches
from subsyncr.core.models import MatchMethod, SyncJob, SyncOptions
from subsyncr.core.planner import build_job, group_key_for
from subsyncr.core.scanner import scan_references, scan_subtitles
from subsyncr.qt.icons import Icon
from subsyncr.qt.palette import ambiguous_color, unmatched_color
from subsyncr.qt.sizing import SIZE_MATCH, SPACING
from subsyncr.qt.theme import ThemeManager
from subsyncr.qt.widgets import (
    SUBTITLE_FILTER,
    EngineCombo,
    PathField,
    enable_row_stripes,
)

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
        self.resize(*SIZE_MATCH)
        self._options = options
        self._output_suffix = output_suffix
        self._rows: list[_Row] = []
        self._extra_note = ""

        self.video_folder = PathField(caption="Select the video folder", parent=self)
        self.video_folder.set_text(video_folder)
        self.subtitle_folder = PathField(
            caption="Select the subtitle folder", parent=self
        )
        self.subtitle_folder.set_text(subtitle_folder)
        self.recursive = QCheckBox("Include subfolders", self)
        self.recursive.setChecked(recursive)
        self.subtitle_reference = QCheckBox("References are subtitles, not video", self)
        self.subtitle_reference.setToolTip(
            "Align subtitles against a known-good subtitle instead of against "
            "audio. This needs no ffmpeg and takes about a tenth of a second "
            "per file."
        )
        self.engine = EngineCombo(options.engine, self)
        self.engine.setToolTip(
            "The engine this batch runs with. The fallback chain from Settings "
            "still applies to any job it fails."
        )
        self.fast_mode = QCheckBox(
            "Fast mode - sample the audio instead of reading the whole file", self
        )
        self.subtitle_reference.toggled.connect(self._update_fast_mode_enabled)
        self.engine.currentIndexChanged.connect(self._update_fast_mode_enabled)
        self.fast_mode.setChecked(fast_mode)
        self.fast_mode.setToolTip(
            "Samples short audio segments when ffsubsync runs, including as a "
            "fallback. Adjust the segment count in Settings > ffsubsync."
        )
        self._update_fast_mode_enabled()
        self.threshold = QDoubleSpinBox(self)
        self.threshold.setRange(0.0, 100.0)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(threshold)
        self.threshold.setToolTip(
            "Fuzzy name similarity a pairing must reach before it is proposed."
        )
        scan_button = QPushButton("Scan", self)
        scan_button.setDefault(True)
        scan_button.clicked.connect(self.scan)
        self._theme = ThemeManager.for_app()
        if self._theme is not None:
            self._theme.register(scan_button, Icon.SCAN)
            # Brushes are baked into the items, so they cannot follow the
            # palette on their own the way the queue table's do.
            self._theme.changed.connect(self._recolor_rows)

        form = QFormLayout()
        form.addRow("Video folder", self.video_folder)
        form.addRow("Subtitle folder", self.subtitle_folder)
        form.addRow("", self.recursive)
        form.addRow("", self.subtitle_reference)
        form.addRow("Engine", self.engine)
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
        enable_row_stripes(self.table)
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

        self._choose_button = QPushButton("Choose subtitle...", self)
        self._choose_button.clicked.connect(self._choose_for_current)
        self._clear_button = QPushButton("Clear pairing", self)
        self._clear_button.clicked.connect(self._clear_selected)
        if self._theme is not None:
            self._theme.register(self._choose_button, Icon.CHOOSE)
            self._theme.register(self._clear_button, Icon.CLEAR)
        self._include_all_button = QPushButton("Queue all matched", self)
        self._include_all_button.clicked.connect(lambda: self._set_include_all(True))
        self._exclude_all_button = QPushButton("Queue none", self)
        self._exclude_all_button.clicked.connect(lambda: self._set_include_all(False))

        actions = QHBoxLayout()
        for button in (
            self._choose_button,
            self._clear_button,
            self._include_all_button,
            self._exclude_all_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        self.table.itemSelectionChanged.connect(self._update_row_actions)

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
        layout.setSpacing(SPACING)
        layout.addWidget(source_box)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(self.summary)
        layout.addWidget(buttons)
        self._update_row_actions()
        # Connected last: the summary reads the table and the OK button.
        self.engine.currentIndexChanged.connect(self._update_summary)

    def _options_for_engine(self) -> SyncOptions:
        return self._options.with_engine(self.engine.engine())

    @Slot()
    def _update_fast_mode_enabled(self) -> None:
        options = self._options_for_engine()
        available = (
            options.engine == EngineId.FFSUBSYNC
            or EngineId.FFSUBSYNC in options.fallback_chain
        )
        self.fast_mode.setEnabled(available and not self.subtitle_reference.isChecked())

    @Slot()
    def _update_row_actions(self) -> None:
        rows = [index for index in self._current_rows() if index < len(self._rows)]
        self._choose_button.setEnabled(len(rows) == 1)
        self._clear_button.setEnabled(
            any(self._rows[index].subtitle is not None for index in rows)
        )
        self._include_all_button.setEnabled(
            any(row.subtitle is not None and not row.include for row in self._rows)
        )
        self._exclude_all_button.setEnabled(any(row.include for row in self._rows))

    # -- scanning --------------------------------------------------------

    @Slot()
    def scan(self) -> None:
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
            video_dir,
            subtitles_as_reference=subtitle_reference,
            recursive=recursive,
            output_suffix=self._output_suffix,
        )
        subtitles = scan_subtitles(
            subtitle_dir, recursive=recursive, output_suffix=self._output_suffix
        )
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
                item.setForeground(QBrush(unmatched_color(self.palette())))
            elif row.ambiguous:
                item.setForeground(QBrush(ambiguous_color(self.palette())))
            self.table.setItem(index, column, item)

    @Slot()
    def _recolor_rows(self) -> None:
        for index, row in enumerate(self._rows):
            self._fill_row(index, row)

    def changeEvent(self, event: QEvent) -> None:
        # Covers a palette change that arrives without a scheme change, such
        # as the dialog being reparented.
        if event.type() is QEvent.Type.PaletteChange and self._rows:
            self._recolor_rows()
        super().changeEvent(event)

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

    @Slot()
    def _update_summary(self) -> None:
        candidates = self._candidates()
        queued = sum(1 for job in candidates if first_capable(job) is not None)
        rerouted = [job for job in candidates if capability_problem(job)]
        stranded = sum(1 for job in rerouted if first_capable(job) is None)
        unmatched = sum(1 for row in self._rows if row.subtitle is None)
        ambiguous = sum(
            1 for row in self._rows if row.subtitle is not None and row.ambiguous
        )
        parts = [f"{queued} of {len(self._rows)} pairings will be queued"]
        if unmatched:
            parts.append(f"{unmatched} videos unmatched")
        if ambiguous:
            parts.append(f"{ambiguous} need a second look")
        engine = engines.get(self.engine.engine()).label
        if stranded:
            parts.append(f"{stranded} skipped: no engine can sync them")
        if len(rerouted) > stranded:
            parts.append(
                f"{len(rerouted) - stranded} will use a fallback: {engine} "
                "cannot handle them"
            )
        self.summary.setText("  -  ".join(parts) + self._extra_note)
        self._ok_button.setEnabled(queued > 0)
        self._update_row_actions()

    # -- result ----------------------------------------------------------

    def jobs(self) -> list[SyncJob]:
        """The pairings to queue, leaving out any that no engine can run."""
        return [job for job in self._candidates() if first_capable(job) is not None]

    def _candidates(self) -> list[SyncJob]:
        # Fast mode is a per-batch decision, so it is applied here rather than
        # written back into the saved settings.
        fast = self.fast_mode.isEnabled() and self.fast_mode.isChecked()
        # with_engine returns a copy, so the fast-mode tweak below stays local.
        options = self._options_for_engine()
        if fast:
            options.ffsubsync.multi_segment_sync = True
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
