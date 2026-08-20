from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ffsubsyncui.domain import SyncJob, SyncOptions, default_output_for
from ffsubsyncui.matching import (
    MEDIA_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    propose_matches,
    scan_folder,
)

MEDIA_FILTER = (
    "Media files (*.mkv *.mp4 *.avi *.mov *.m4v *.webm "
    "*.ts *.m2ts *.mpg *.mpeg);;All files (*)"
)
SUBTITLE_FILTER = "Subtitle files (*.srt *.ass *.ssa *.sub);;All files (*)"


class PathField(QWidget):
    def __init__(
        self,
        *,
        directory: bool = False,
        file_filter: str = "All files (*)",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.directory = directory
        self.file_filter = file_filter
        self.edit = QLineEdit(self)
        self.button = QPushButton("Browse…", self)
        self.button.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def _browse(self) -> None:
        if self.directory:
            selected = QFileDialog.getExistingDirectory(
                self, "Select folder", self.text()
            )
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self, "Select file", self.text(), self.file_filter
            )
        if selected:
            self.setText(selected)


class SettingsDialog(QDialog):
    def __init__(self, options: SyncOptions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sync settings")
        self.resize(560, 360)

        self.ffmpeg_path = PathField(parent=self)
        self.ffmpeg_path.setText(options.ffmpeg_path)
        self.vad = QComboBox(self)
        self.vad.addItems(
            ("subs_then_webrtc", "webrtc", "subs_then_auditok", "auditok")
        )
        self.vad.setCurrentText(options.vad)
        self.max_offset = QDoubleSpinBox(self)
        self.max_offset.setRange(0.0, 3600.0)
        self.max_offset.setSuffix(" seconds")
        self.max_offset.setValue(options.max_offset_seconds)
        self.encoding = QComboBox(self)
        self.encoding.setEditable(True)
        self.encoding.addItems(("infer", "utf-8", "windows-1252", "latin-1"))
        self.encoding.setCurrentText(options.encoding)
        self.output_encoding = QComboBox(self)
        self.output_encoding.setEditable(True)
        self.output_encoding.addItems(("utf-8", "same", "windows-1252"))
        self.output_encoding.setCurrentText(options.output_encoding)
        self.no_fix_framerate = QCheckBox("Disable framerate correction", self)
        self.no_fix_framerate.setChecked(options.no_fix_framerate)
        self.quality_gate = QCheckBox("Reject low-quality batch alignments", self)
        self.quality_gate.setChecked(options.quality_gate)
        self.reference_stream = QLineEdit(options.reference_stream, self)
        self.reference_stream.setPlaceholderText("Example: a:0 or s:1")

        form = QFormLayout()
        form.addRow("FFmpeg directory or executable", self.ffmpeg_path)
        form.addRow("Voice activity detector", self.vad)
        form.addRow("Maximum offset", self.max_offset)
        form.addRow("Input encoding", self.encoding)
        form.addRow("Output encoding", self.output_encoding)
        form.addRow("Reference stream", self.reference_stream)
        form.addRow("", self.no_fix_framerate)
        form.addRow("", self.quality_gate)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(buttons)

    def options(self) -> SyncOptions:
        return SyncOptions(
            encoding=self.encoding.currentText().strip() or "infer",
            output_encoding=self.output_encoding.currentText().strip() or "utf-8",
            vad=self.vad.currentText(),
            max_offset_seconds=self.max_offset.value(),
            no_fix_framerate=self.no_fix_framerate.isChecked(),
            quality_gate=self.quality_gate.isChecked(),
            reference_stream=self.reference_stream.text().strip(),
            ffmpeg_path=self.ffmpeg_path.text(),
        )


class AddJobDialog(QDialog):
    def __init__(self, defaults: SyncOptions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.defaults = defaults
        self.setWindowTitle("Add sync job")
        self.resize(700, 280)
        self.reference = PathField(file_filter=MEDIA_FILTER, parent=self)
        self.subtitle = PathField(file_filter=SUBTITLE_FILTER, parent=self)
        self.output = PathField(
            file_filter="SRT files (*.srt);;All files (*)", parent=self
        )
        self.subtitle.edit.textChanged.connect(self._derive_output)
        self.overwrite_input = QCheckBox("Overwrite the input subtitle", self)
        self.overwrite_input.toggled.connect(self.output.setDisabled)
        self.replace_output = QCheckBox("Replace an existing output file", self)

        form = QFormLayout()
        form.addRow("Reference media", self.reference)
        form.addRow("Input subtitle", self.subtitle)
        form.addRow("Output subtitle", self.output)
        form.addRow("", self.overwrite_input)
        form.addRow("", self.replace_output)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(buttons)

    def _derive_output(self, value: str) -> None:
        path = Path(value.strip())
        if value.strip():
            self.output.setText(str(default_output_for(path)))

    def _accept_if_valid(self) -> None:
        if not self.reference.text() or not self.subtitle.text():
            QMessageBox.warning(
                self, "Missing paths", "Choose a reference and subtitle file."
            )
            return
        if not self.overwrite_input.isChecked() and not self.output.text():
            QMessageBox.warning(
                self, "Missing output", "Choose an output subtitle path."
            )
            return
        self.accept()

    def job(self) -> SyncJob:
        subtitle = Path(self.subtitle.text())
        overwrite = self.overwrite_input.isChecked()
        options = replace(
            self.defaults,
            overwrite_input=overwrite,
            replace_output=self.replace_output.isChecked(),
        )
        return SyncJob(
            reference_path=Path(self.reference.text()),
            subtitle_path=subtitle,
            output_path=subtitle if overwrite else Path(self.output.text()),
            options=options,
        )


class BatchMatchDialog(QDialog):
    def __init__(self, defaults: SyncOptions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.defaults = defaults
        self._subtitles: list[Path] = []
        self.setWindowTitle("Match media and subtitles")
        self.resize(1000, 650)

        self.media_folder = PathField(directory=True, parent=self)
        self.subtitle_folder = PathField(directory=True, parent=self)
        self.recursive = QCheckBox("Include subfolders", self)
        self.threshold = QSpinBox(self)
        self.threshold.setRange(0, 100)
        self.threshold.setValue(75)
        self.threshold.setSuffix("%")
        self.match_button = QPushButton("Scan and match", self)
        self.match_button.clicked.connect(self._match)
        self.summary = QLabel("Choose folders, then review every proposed pair.", self)

        folder_form = QFormLayout()
        folder_form.addRow("Media folder", self.media_folder)
        folder_form.addRow("Subtitle folder", self.subtitle_folder)
        controls = QHBoxLayout()
        controls.addWidget(self.recursive)
        controls.addWidget(QLabel("Fuzzy threshold", self))
        controls.addWidget(self.threshold)
        controls.addStretch()
        controls.addWidget(self.match_button)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ("Reference", "Matched subtitle", "Method", "Score", "Review note")
        )
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add matched jobs")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(folder_form)
        layout.addLayout(controls)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)

    def _match(self) -> None:
        media_dir = Path(self.media_folder.text())
        subtitle_dir = Path(self.subtitle_folder.text())
        if not media_dir.is_dir() or not subtitle_dir.is_dir():
            QMessageBox.warning(
                self, "Invalid folders", "Choose existing media and subtitle folders."
            )
            return
        recursive = self.recursive.isChecked()
        references = scan_folder(media_dir, MEDIA_EXTENSIONS, recursive=recursive)
        self._subtitles = scan_folder(
            subtitle_dir, SUBTITLE_EXTENSIONS, recursive=recursive
        )
        proposals = propose_matches(
            references, self._subtitles, threshold=float(self.threshold.value())
        )
        self.table.setRowCount(len(proposals))
        matched = 0
        for row, proposal in enumerate(proposals):
            reference_item = QTableWidgetItem(proposal.reference_path.name)
            reference_item.setData(
                Qt.ItemDataRole.UserRole, str(proposal.reference_path)
            )
            reference_item.setToolTip(str(proposal.reference_path))
            reference_item.setFlags(
                reference_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            self.table.setItem(row, 0, reference_item)

            combo = QComboBox(self.table)
            combo.addItem("— Unmatched —", None)
            for subtitle in self._subtitles:
                combo.addItem(subtitle.name, str(subtitle))
            if proposal.subtitle_path is not None:
                index = combo.findData(str(proposal.subtitle_path))
                combo.setCurrentIndex(index)
                matched += 1
            self.table.setCellWidget(row, 1, combo)
            self._set_read_only(row, 2, proposal.method.value)
            self._set_read_only(
                row, 3, f"{proposal.score:.1f}%" if proposal.score else "—"
            )
            self._set_read_only(row, 4, proposal.ambiguity_reason)
            combo.currentIndexChanged.connect(
                lambda _index, current_row=row: self._mark_manual(current_row)
            )
        self.summary.setText(
            f"Found {len(references)} media files and "
            f"{len(self._subtitles)} subtitles; "
            f"proposed {matched} pairs. Review each row before adding jobs."
        )

    def _set_read_only(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, column, item)

    def _mark_manual(self, row: int) -> None:
        combo = self.table.cellWidget(row, 1)
        if not isinstance(combo, QComboBox):
            return
        self._set_read_only(row, 2, "manual")
        self._set_read_only(row, 3, "-")
        note = "Manually selected" if combo.currentData() is not None else "Not queued"
        self._set_read_only(row, 4, note)

    def _accept_if_valid(self) -> None:
        selected: list[str] = []
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            if isinstance(combo, QComboBox) and combo.currentData() is not None:
                selected.append(str(combo.currentData()))
        if not selected:
            QMessageBox.warning(self, "No matches", "Assign at least one subtitle.")
            return
        if len(selected) != len(set(selected)):
            QMessageBox.warning(
                self, "Duplicate assignment", "Each subtitle can only be assigned once."
            )
            return
        self.accept()

    def jobs(self) -> list[SyncJob]:
        jobs: list[SyncJob] = []
        for row in range(self.table.rowCount()):
            reference_item = self.table.item(row, 0)
            combo = self.table.cellWidget(row, 1)
            if reference_item is None or not isinstance(combo, QComboBox):
                continue
            subtitle_value = combo.currentData()
            if subtitle_value is None:
                continue
            subtitle = Path(str(subtitle_value))
            jobs.append(
                SyncJob(
                    reference_path=Path(
                        str(reference_item.data(Qt.ItemDataRole.UserRole))
                    ),
                    subtitle_path=subtitle,
                    output_path=default_output_for(subtitle),
                    options=replace(self.defaults),
                )
            )
        return jobs
