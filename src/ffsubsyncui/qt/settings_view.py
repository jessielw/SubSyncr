"""Settings dialog covering the whole ffsubsync option surface."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ffsubsyncui.core.config import MAX_CONCURRENCY, AppConfig, speech_cache_dir
from ffsubsyncui.core.ffmpeg import probe
from ffsubsyncui.core.models import VAD_CHOICES, SyncOptions
from ffsubsyncui.core.speech_cache import SpeechCache


class _OptionalDouble(QWidget):
    """A spin box that can also mean "not set", for ffsubsync's optional args."""

    def __init__(
        self,
        value: float | None,
        *,
        minimum: float,
        maximum: float,
        label: str,
        suffix: str = "",
        default: float = 0.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.toggle = QCheckBox(label, self)
        self.spin = QDoubleSpinBox(self)
        self.spin.setRange(minimum, maximum)
        if suffix:
            self.spin.setSuffix(suffix)
        self.spin.setValue(value if value is not None else default)
        self.toggle.setChecked(value is not None)
        self.spin.setEnabled(value is not None)
        self.toggle.toggled.connect(self.spin.setEnabled)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.spin, 1)

    def value(self) -> float | None:
        return self.spin.value() if self.toggle.isChecked() else None


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(660, 520)
        self._config = config
        options = config.sync

        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(config), "General")
        tabs.addTab(self._alignment_tab(options), "Alignment")
        tabs.addTab(self._quality_tab(options), "Quality gate")
        tabs.addTab(self._files_tab(options), "Files")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- tabs ------------------------------------------------------------

    def _general_tab(self, config: AppConfig) -> QWidget:
        page = QWidget(self)
        self.concurrency = QSpinBox(page)
        self.concurrency.setRange(1, MAX_CONCURRENCY)
        self.concurrency.setValue(config.concurrency)
        self.concurrency.setToolTip(
            "Videos to process at once. Each is a separate process, so memory "
            "is returned as soon as it finishes and any one can be cancelled. "
            "All the subtitles for one video share a single process, and a "
            "single audio extraction."
        )
        self.match_threshold = QDoubleSpinBox(page)
        self.match_threshold.setRange(0.0, 100.0)
        self.match_threshold.setSuffix(" %")
        self.match_threshold.setValue(config.match_threshold)
        self.recursive_scan = QCheckBox("Scan subfolders by default", page)
        self.recursive_scan.setChecked(config.recursive_scan)
        self.auto_start = QCheckBox("Start the queue as soon as jobs are added", page)
        self.auto_start.setChecked(config.auto_start)
        self.output_suffix = QLineEdit(config.output_suffix, page)
        self.output_suffix.setToolTip(
            "Inserted before the extension, so ep.en.srt becomes ep.en.synced.srt."
        )
        self.history_limit = QSpinBox(page)
        self.history_limit.setRange(0, 100000)
        self.history_limit.setValue(config.history_limit)

        self.speech_cache = QCheckBox("Remember extracted speech between runs", page)
        self.speech_cache.setChecked(config.speech_cache_enabled)
        self.speech_cache.setToolTip(
            "Subtitles queued together already share one extraction. This keeps "
            "it on disk as well, so syncing another language for the same video "
            "in a later session skips the audio pass entirely."
        )
        self._cache_label = QLabel(page)
        clear_cache = QPushButton("Clear cache", page)
        clear_cache.clicked.connect(self._clear_cache)
        cache_row = QHBoxLayout()
        cache_row.addWidget(self._cache_label, 1)
        cache_row.addWidget(clear_cache)
        self._update_cache_label()

        form = QFormLayout(page)
        form.addRow("Concurrent jobs", self.concurrency)
        form.addRow("Default similarity threshold", self.match_threshold)
        form.addRow("", self.recursive_scan)
        form.addRow("", self.auto_start)
        form.addRow("Output suffix", self.output_suffix)
        form.addRow("History entries to keep", self.history_limit)
        form.addRow("", self.speech_cache)
        form.addRow("Speech cache", cache_row)
        return page

    def _alignment_tab(self, options: SyncOptions) -> QWidget:
        page = QWidget(self)
        self.vad = QComboBox(page)
        self.vad.addItems(VAD_CHOICES)
        self.vad.setCurrentText(options.vad)
        self.vad.setToolTip(
            "subs_then_* first tries an embedded text subtitle track, which "
            "needs no audio decoding at all. auditok keys on energy rather "
            "than voice and copes better with poor audio."
        )
        self.max_offset = QDoubleSpinBox(page)
        self.max_offset.setRange(0.0, 3600.0)
        self.max_offset.setSuffix(" s")
        self.max_offset.setValue(options.max_offset_seconds)
        self.no_fix_framerate = QCheckBox("Do not correct framerate", page)
        self.no_fix_framerate.setChecked(options.no_fix_framerate)
        self.gss = QCheckBox("Golden-section search for framerate ratio", page)
        self.gss.setChecked(options.gss)
        self.reference_stream = QLineEdit(options.reference_stream, page)
        self.reference_stream.setPlaceholderText("for example a:0 or s:1")

        self.split_penalty = _OptionalDouble(
            options.split_penalty,
            minimum=0.0,
            maximum=100.0,
            label="Piecewise alignment",
            default=5.0,
            parent=page,
        )
        self.split_penalty.setToolTip(
            "alass-style variable offset. Use when a subtitle drifts across "
            "the runtime instead of being off by a constant amount."
        )
        self.multi_segment = QCheckBox(
            "Sample segments instead of the whole file", page
        )
        self.multi_segment.setChecked(options.multi_segment_sync)
        self.segment_count = QSpinBox(page)
        self.segment_count.setRange(2, 64)
        self.segment_count.setValue(options.segment_count)
        self.segment_count.setEnabled(options.multi_segment_sync)
        self.multi_segment.toggled.connect(self.segment_count.setEnabled)
        self.max_duration = _OptionalDouble(
            options.max_duration_seconds,
            minimum=1.0,
            maximum=100000.0,
            label="Limit reference length",
            suffix=" s",
            default=600.0,
            parent=page,
        )
        self.start_seconds = QSpinBox(page)
        self.start_seconds.setRange(0, 100000)
        self.start_seconds.setSuffix(" s")
        self.start_seconds.setValue(options.start_seconds)

        form = QFormLayout(page)
        form.addRow("Speech detector", self.vad)
        form.addRow("Maximum offset", self.max_offset)
        form.addRow("Reference stream", self.reference_stream)
        form.addRow("Skip start of reference", self.start_seconds)
        form.addRow("", self.no_fix_framerate)
        form.addRow("", self.gss)
        form.addRow("", self.split_penalty)
        form.addRow("", self.multi_segment)
        form.addRow("Segments", self.segment_count)
        form.addRow("", self.max_duration)
        return page

    def _quality_tab(self, options: SyncOptions) -> QWidget:
        page = QWidget(self)
        self.skip_low_quality = QCheckBox(
            "Leave subtitles unmodified when the alignment looks wrong", page
        )
        self.skip_low_quality.setChecked(options.skip_sync_on_low_quality)
        self.skip_low_quality.setToolTip(
            "ffsubsync writes the original subtitles to the output path and "
            "the job is reported as Low quality rather than Done."
        )
        self.min_score = QDoubleSpinBox(page)
        self.min_score.setRange(-1e9, 1e9)
        self.min_score.setDecimals(1)
        self.min_score.setValue(options.min_score)
        self.min_score.setToolTip(
            "The magnitude is not normalized, but a negative score means the "
            "best alignment found was anti-correlated."
        )
        self.quality_max_offset = QDoubleSpinBox(page)
        self.quality_max_offset.setRange(0.0, 3600.0)
        self.quality_max_offset.setSuffix(" s")
        self.quality_max_offset.setValue(options.quality_max_offset_seconds)
        self.max_framerate_deviation = QDoubleSpinBox(page)
        self.max_framerate_deviation.setRange(0.0, 1.0)
        self.max_framerate_deviation.setDecimals(3)
        self.max_framerate_deviation.setSingleStep(0.01)
        self.max_framerate_deviation.setValue(options.max_framerate_deviation)

        for widget in (
            self.min_score,
            self.quality_max_offset,
            self.max_framerate_deviation,
        ):
            widget.setEnabled(options.skip_sync_on_low_quality)
            self.skip_low_quality.toggled.connect(widget.setEnabled)

        form = QFormLayout(page)
        form.addRow("", self.skip_low_quality)
        form.addRow("Minimum score", self.min_score)
        form.addRow("Implausible offset above", self.quality_max_offset)
        form.addRow(
            "Implausible framerate deviation above", self.max_framerate_deviation
        )
        return page

    def _files_tab(self, options: SyncOptions) -> QWidget:
        page = QWidget(self)
        self.encoding = QComboBox(page)
        self.encoding.setEditable(True)
        self.encoding.addItems(("infer", "utf-8", "windows-1252", "latin-1", "cp1251"))
        self.encoding.setCurrentText(options.encoding)
        self.output_encoding = QComboBox(page)
        self.output_encoding.setEditable(True)
        self.output_encoding.addItems(("utf-8", "same", "windows-1252"))
        self.output_encoding.setCurrentText(options.output_encoding)
        self.apply_offset = QDoubleSpinBox(page)
        self.apply_offset.setRange(-3600.0, 3600.0)
        self.apply_offset.setDecimals(3)
        self.apply_offset.setSuffix(" s")
        self.apply_offset.setValue(options.apply_offset_seconds)
        self.apply_offset.setToolTip(
            "Added to whatever offset the alignment finds. Usually zero."
        )
        self.replace_output = QCheckBox("Allow replacing an existing output file", page)
        self.replace_output.setChecked(options.replace_output)
        self.backup_before_overwrite = QCheckBox(
            "Keep a .bak copy before overwriting a subtitle in place", page
        )
        self.backup_before_overwrite.setChecked(options.backup_before_overwrite)
        self.backup_before_overwrite.setToolTip(
            "Only applies when a job writes over its input. An existing .bak is "
            "never replaced, so re-running cannot bury the true original."
        )

        self.ffmpeg_path = QLineEdit(options.ffmpeg_path, page)
        self.ffmpeg_path.setPlaceholderText("leave empty to use PATH")
        browse = QPushButton("Browse...", page)
        browse.clicked.connect(self._browse_ffmpeg)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_path, 1)
        ffmpeg_row.addWidget(browse)
        self._ffmpeg_status = QLabel(page)
        self._ffmpeg_status.setWordWrap(True)
        self.ffmpeg_path.textChanged.connect(self._update_ffmpeg_status)
        self._update_ffmpeg_status()

        form = QFormLayout(page)
        form.addRow("Input encoding", self.encoding)
        form.addRow("Output encoding", self.output_encoding)
        form.addRow("Extra offset", self.apply_offset)
        form.addRow("", self.replace_output)
        form.addRow("", self.backup_before_overwrite)
        form.addRow("ffmpeg folder", ffmpeg_row)
        form.addRow("", self._ffmpeg_status)
        return page

    # -- actions ---------------------------------------------------------

    @Slot()
    def _browse_ffmpeg(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Select the folder containing ffmpeg", self.ffmpeg_path.text()
        )
        if selected:
            self.ffmpeg_path.setText(selected)

    @Slot()
    def _update_ffmpeg_status(self) -> None:
        status = probe(self.ffmpeg_path.text())
        self._ffmpeg_status.setText(status.message)
        self._ffmpeg_status.setStyleSheet(
            "color: #15803d;" if status.ready else "color: #b45309;"
        )

    def _update_cache_label(self) -> None:
        megabytes = SpeechCache(speech_cache_dir()).size_bytes() / (1024 * 1024)
        self._cache_label.setText(f"{megabytes:.1f} MB stored")

    @Slot()
    def _clear_cache(self) -> None:
        removed = SpeechCache(speech_cache_dir()).clear()
        self._update_cache_label()
        QMessageBox.information(
            self, "Speech cache", f"Removed {removed} cached speech arrays."
        )

    # -- result ----------------------------------------------------------

    def config(self) -> AppConfig:
        sync = replace(
            self._config.sync,
            vad=self.vad.currentText(),
            max_offset_seconds=self.max_offset.value(),
            no_fix_framerate=self.no_fix_framerate.isChecked(),
            gss=self.gss.isChecked(),
            reference_stream=self.reference_stream.text().strip(),
            split_penalty=self.split_penalty.value(),
            multi_segment_sync=self.multi_segment.isChecked(),
            segment_count=self.segment_count.value(),
            max_duration_seconds=self.max_duration.value(),
            start_seconds=self.start_seconds.value(),
            skip_sync_on_low_quality=self.skip_low_quality.isChecked(),
            min_score=self.min_score.value(),
            quality_max_offset_seconds=self.quality_max_offset.value(),
            max_framerate_deviation=self.max_framerate_deviation.value(),
            encoding=self.encoding.currentText().strip() or "infer",
            output_encoding=self.output_encoding.currentText().strip() or "utf-8",
            replace_output=self.replace_output.isChecked(),
            backup_before_overwrite=self.backup_before_overwrite.isChecked(),
            apply_offset_seconds=self.apply_offset.value(),
            ffmpeg_path=self.ffmpeg_path.text().strip(),
        )
        updated = AppConfig(
            sync=sync,
            concurrency=self.concurrency.value(),
            match_threshold=self.match_threshold.value(),
            recursive_scan=self.recursive_scan.isChecked(),
            output_suffix=self.output_suffix.text().strip(),
            speech_cache_enabled=self.speech_cache.isChecked(),
            history_limit=self.history_limit.value(),
            auto_start=self.auto_start.isChecked(),
        )
        return updated.normalized()
