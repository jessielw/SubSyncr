"""Settings dialog: general behaviour, and every engine's option surface.

Each engine gets its own tab, and all of them keep their settings whether or
not they are the one selected - switching engines to try something never
costs you the configuration you had.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from subsyncr.core import binaries, engines
from subsyncr.core.config import (
    MAX_CONCURRENCY,
    AppConfig,
    Theme,
    cache_dir,
    speech_cache_dir,
)
from subsyncr.core.engines import EngineId, EngineKind
from subsyncr.core.models import (
    LAPSE_MODES,
    VAD_CHOICES,
    AlassOptions,
    AutoSubSyncOptions,
    FFSubSyncOptions,
    LapseOptions,
    SyncOptions,
)
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.qt.icons import Icon
from subsyncr.qt.sizing import (
    FALLBACK_LIST_HEIGHT,
    SIZE_SETTINGS,
    SPACING,
)
from subsyncr.qt.theme import THEME_LABELS, ThemeManager, apply_theme
from subsyncr.qt.widgets import FFmpegStatusLabel, PathField


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
        self.resize(*SIZE_SETTINGS)
        self._theme = ThemeManager.for_app()
        self._config = config
        # Theme changes preview live; restored if the dialog is cancelled.
        self._original_theme = config.theme
        options = config.sync

        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(config), "General")
        tabs.addTab(self._engine_tab(options), "Engine")
        tabs.addTab(self._quality_tab(options.ffsubsync), "Quality gate")
        tabs.addTab(self._alignment_tab(options.ffsubsync), "ffsubsync")
        tabs.addTab(self._alass_tab(options.alass), "alass")
        tabs.addTab(self._lapse_tab(options.lapse), "lapse")
        tabs.addTab(self._autosubsync_tab(options.autosubsync), "autosubsync")
        tabs.addTab(self._files_tab(options), "Files")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- tabs ------------------------------------------------------------

    def _general_tab(self, config: AppConfig) -> QWidget:
        page = QWidget(self)
        self.theme = QComboBox(page)
        for choice in Theme:
            self.theme.addItem(THEME_LABELS[choice], choice)
        self.theme.setCurrentIndex(self.theme.findData(config.theme))
        self.theme.setToolTip(
            "Automatic follows the OS setting and switches live with it."
        )
        self.theme.currentIndexChanged.connect(self._preview_theme)

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
        if self._theme is not None:
            self._theme.register(clear_cache, Icon.CLEAR)
        cache_row = QHBoxLayout()
        cache_row.setSpacing(SPACING)
        cache_row.addWidget(self._cache_label, 1)
        cache_row.addWidget(clear_cache)
        self._update_cache_label()

        form = QFormLayout(page)
        form.addRow("Theme", self.theme)
        form.addRow("Concurrent jobs", self.concurrency)
        form.addRow("Default similarity threshold", self.match_threshold)
        form.addRow("", self.recursive_scan)
        form.addRow("", self.auto_start)
        form.addRow("Output suffix", self.output_suffix)
        form.addRow("History entries to keep", self.history_limit)
        form.addRow("", self.speech_cache)
        form.addRow("Speech cache", cache_row)
        return page

    def _engine_tab(self, options: SyncOptions) -> QWidget:
        page = QWidget(self)
        self.engine = QComboBox(page)
        for spec in engines.all_specs():
            self.engine.addItem(spec.label, spec.id)
        self.engine.setCurrentIndex(self.engine.findData(options.engine))
        self.engine.setToolTip(
            "ffsubsync detects speech in the audio. alass is strong on subtitles "
            "that drift across advert breaks. lapse is fast and reports how "
            "confident it is. autosubsync uses a trained classifier and needs the "
            "audio, so it cannot align against another subtitle."
        )
        self.engine.currentIndexChanged.connect(self._update_engine_status)
        self._engine_status = QLabel(page)
        self._engine_status.setWordWrap(True)

        self.extract_embedded = QCheckBox(
            "Prefer a subtitle track inside the video over its audio", page
        )
        self.extract_embedded.setChecked(options.extract_embedded)
        self.extract_embedded.setToolTip(
            "When the video carries a text subtitle track, align against that "
            "rather than detecting speech. Far faster, and usually more accurate, "
            "because the timings are the release's own. Falls back to the audio "
            "when there is no usable track."
        )

        self.fallback = QListWidget(page)
        self.fallback.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.fallback.setToolTip(
            "Tried in order when the chosen engine fails, or reports an alignment "
            "it does not trust. Nothing ticked means a failure is final."
        )
        for spec in engines.all_specs():
            item = QListWidgetItem(spec.label, self.fallback)
            item.setData(Qt.ItemDataRole.UserRole, spec.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if spec.id in options.fallback_chain
                else Qt.CheckState.Unchecked
            )
        self.fallback.setMaximumHeight(FALLBACK_LIST_HEIGHT)

        form = QFormLayout()
        form.addRow("Sync engine", self.engine)
        form.addRow("", self._engine_status)
        form.addRow("", self.extract_embedded)
        form.addRow("Fall back to", self.fallback)
        layout = QVBoxLayout(page)
        layout.addLayout(form)
        layout.addStretch(1)
        self._update_engine_status()
        return page

    def _alass_tab(self, options: AlassOptions) -> QWidget:
        page = QWidget(self)
        self.alass_split_penalty = _OptionalDouble(
            options.split_penalty,
            minimum=0.0,
            maximum=1000.0,
            label="Split into segments",
            default=7.0,
            parent=page,
        )
        self.alass_split_penalty.setToolTip(
            "How reluctant alass is to cut the subtitle into independently "
            "shifted pieces. 1 to 20 is the useful range and its own default is "
            "7. Unticked passes --no-split, which only shifts and is much faster."
        )
        self.alass_fps_guessing = QCheckBox("Do not guess framerate", page)
        self.alass_fps_guessing.setChecked(options.disable_fps_guessing)
        self.alass_speed = QCheckBox(
            "Disable speed optimization (slower, more accurate)", page
        )
        self.alass_speed.setChecked(options.disable_speed_optimization)
        self.alass_interval = QSpinBox(page)
        self.alass_interval.setRange(1, 10000)
        self.alass_interval.setSuffix(" ms")
        self.alass_interval.setValue(options.interval_ms)
        self.alass_interval.setToolTip(
            "The smallest difference alass will resolve. Smaller is more "
            "accurate and slower."
        )
        self.alass_negative = QCheckBox("Allow negative timestamps", page)
        self.alass_negative.setChecked(options.allow_negative_timestamps)
        self.alass_negative.setToolTip(
            "Off by default because some players refuse a subtitle that starts "
            "before zero; alass clamps to zero instead."
        )

        form = QFormLayout(page)
        form.addRow("", self.alass_split_penalty)
        form.addRow("Resolution", self.alass_interval)
        form.addRow("", self.alass_fps_guessing)
        form.addRow("", self.alass_speed)
        form.addRow("", self.alass_negative)
        return page

    def _lapse_tab(self, options: LapseOptions) -> QWidget:
        page = QWidget(self)
        self.lapse_mode = QComboBox(page)
        self.lapse_mode.addItems(LAPSE_MODES)
        self.lapse_mode.setCurrentText(options.mode)
        self.lapse_mode.setToolTip(
            "auto lets lapse decide. split allows independently shifted segments, "
            "nosplit forces a single offset, ols fits a linear drift."
        )
        self.lapse_split_penalty = QDoubleSpinBox(page)
        self.lapse_split_penalty.setRange(1.0, 50.0)
        self.lapse_split_penalty.setValue(options.split_penalty)
        self.lapse_confidence = QDoubleSpinBox(page)
        self.lapse_confidence.setRange(0.0, 100.0)
        self.lapse_confidence.setValue(options.confidence)
        self.lapse_confidence.setToolTip(
            "How far lapse's answer has to stand out before it will write. "
            "Higher refuses more; its own default is 8."
        )
        self.lapse_force = QCheckBox("Apply the shift even when lapse is unsure", page)
        self.lapse_force.setChecked(options.force)
        self.lapse_force.setToolTip(
            "Off by default, so an uncertain result leaves the original alone and "
            "the job is reported as Low quality rather than Done."
        )
        self.lapse_embedded = QCheckBox("Let lapse use embedded subtitles", page)
        self.lapse_embedded.setChecked(options.use_embedded)
        self.lapse_full_scan = QCheckBox("Full scan", page)
        self.lapse_full_scan.setChecked(options.full_scan)
        self.lapse_no_cache = QCheckBox("Ignore lapse's own analysis cache", page)
        self.lapse_no_cache.setChecked(options.no_cache)

        form = QFormLayout(page)
        form.addRow("Mode", self.lapse_mode)
        form.addRow("Split penalty", self.lapse_split_penalty)
        form.addRow("Confidence needed", self.lapse_confidence)
        form.addRow("", self.lapse_force)
        form.addRow("", self.lapse_embedded)
        form.addRow("", self.lapse_full_scan)
        form.addRow("", self.lapse_no_cache)
        return page

    def _autosubsync_tab(self, options: AutoSubSyncOptions) -> QWidget:
        page = QWidget(self)
        self.autosubsync_max_shift = QDoubleSpinBox(page)
        self.autosubsync_max_shift.setRange(1.0, 600.0)
        self.autosubsync_max_shift.setSuffix(" s")
        self.autosubsync_max_shift.setValue(options.max_shift_secs)
        self.autosubsync_parallelism = QSpinBox(page)
        self.autosubsync_parallelism.setRange(1, 16)
        self.autosubsync_parallelism.setValue(options.parallelism)
        self.autosubsync_parallelism.setToolTip(
            "Worker processes for feature extraction. This multiplies with the "
            "number of concurrent jobs, so keep it modest on a busy machine."
        )
        note = QLabel(
            "autosubsync classifies the audio, so it needs a video reference and "
            "reads SubRip only. Other formats are converted for it automatically.",
            page,
        )
        note.setWordWrap(True)

        form = QFormLayout(page)
        form.addRow("Maximum shift", self.autosubsync_max_shift)
        form.addRow("Parallel workers", self.autosubsync_parallelism)
        form.addRow("", note)
        return page

    @Slot()
    def _update_engine_status(self) -> None:
        """Say whether the selected engine can actually run on this machine."""
        chosen = self.engine.currentData()
        if chosen is None:
            return
        for index in range(self.fallback.count()):
            item = self.fallback.item(index)
            active = item.data(Qt.ItemDataRole.UserRole) == chosen
            if active:
                item.setCheckState(Qt.CheckState.Unchecked)
            item.setFlags(
                item.flags() & ~Qt.ItemFlag.ItemIsEnabled
                if active
                else item.flags() | Qt.ItemFlag.ItemIsEnabled
            )
        spec = engines.get(chosen)
        if spec.kind is not EngineKind.BINARY:
            self._engine_status.hide()
            return
        binary = binaries.spec_for(spec.id)
        if binary is None:
            self._engine_status.hide()
            return
        self._engine_status.setText(binaries.status(binary, cache_dir()).message)
        self._engine_status.show()

    def _alignment_tab(self, options: FFSubSyncOptions) -> QWidget:
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

    def _quality_tab(self, options: FFSubSyncOptions) -> QWidget:
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
        self.encoding.setCurrentText(options.files.encoding)
        self.output_encoding = QComboBox(page)
        self.output_encoding.setEditable(True)
        self.output_encoding.addItems(("utf-8", "same", "windows-1252"))
        self.output_encoding.setCurrentText(options.files.output_encoding)
        self.apply_offset = QDoubleSpinBox(page)
        self.apply_offset.setRange(-3600.0, 3600.0)
        self.apply_offset.setDecimals(3)
        self.apply_offset.setSuffix(" s")
        self.apply_offset.setValue(options.ffsubsync.apply_offset_seconds)
        self.apply_offset.setToolTip(
            "Added to whatever offset the alignment finds. Usually zero."
        )
        self.replace_output = QCheckBox("Allow replacing an existing output file", page)
        self.replace_output.setChecked(options.files.replace_output)
        self.backup_before_overwrite = QCheckBox(
            "Keep a .bak copy before overwriting a subtitle in place", page
        )
        self.backup_before_overwrite.setChecked(options.files.backup_before_overwrite)
        self.backup_before_overwrite.setToolTip(
            "Only applies when a job writes over its input. An existing .bak is "
            "never replaced, so re-running cannot bury the true original."
        )

        self.ffmpeg_field = PathField(
            caption="Select the folder containing ffmpeg",
            on_change=self._update_ffmpeg_status,
            parent=page,
        )
        self.ffmpeg_field.set_text(options.environment.ffmpeg_path)
        self.ffmpeg_field.edit.setPlaceholderText("leave empty to use PATH")
        self._ffmpeg_status = FFmpegStatusLabel(page)
        self._update_ffmpeg_status()

        form = QFormLayout(page)
        form.addRow("Input encoding", self.encoding)
        form.addRow("Output encoding", self.output_encoding)
        form.addRow("Extra offset", self.apply_offset)
        form.addRow("", self.replace_output)
        form.addRow("", self.backup_before_overwrite)
        form.addRow("ffmpeg folder", self.ffmpeg_field)
        form.addRow("", self._ffmpeg_status)
        return page

    # -- actions ---------------------------------------------------------

    @Slot(int)
    def _preview_theme(self, _index: int) -> None:
        apply_theme(Theme(self.theme.currentData()))

    def reject(self) -> None:
        # Escape, the window's close button and Cancel all end up here, so
        # this is the one place a live-previewed theme needs undoing.
        apply_theme(self._original_theme)
        super().reject()

    @Slot()
    def _update_ffmpeg_status(self) -> None:
        self._ffmpeg_status.refresh(self.ffmpeg_field.text())

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
        # Start from a copy so engines this dialog does not yet edit keep the
        # settings the user already has.
        sync = self._config.sync.copy()
        sync.ffsubsync = replace(
            sync.ffsubsync,
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
            apply_offset_seconds=self.apply_offset.value(),
        )
        sync.files = replace(
            sync.files,
            encoding=self.encoding.currentText().strip() or "infer",
            output_encoding=self.output_encoding.currentText().strip() or "utf-8",
            replace_output=self.replace_output.isChecked(),
            backup_before_overwrite=self.backup_before_overwrite.isChecked(),
        )
        sync.environment = replace(
            sync.environment, ffmpeg_path=self.ffmpeg_field.text()
        )
        # Qt stores an ``EngineId`` as a plain string, because it is one, and
        # hands it back that way - so everything read out of a widget has to be
        # parsed back into the enum before it reaches an option block.
        sync.engine = EngineId.parse(self.engine.currentData(), sync.engine)
        sync.extract_embedded = self.extract_embedded.isChecked()
        sync.fallback_chain = tuple(
            fallback
            for index in range(self.fallback.count())
            if (item := self.fallback.item(index)).checkState() is Qt.CheckState.Checked
            # The active engine is already the first thing tried.
            and (fallback := EngineId.parse(item.data(Qt.ItemDataRole.UserRole)))
            != sync.engine
        )
        sync.alass = replace(
            sync.alass,
            split_penalty=self.alass_split_penalty.value(),
            disable_fps_guessing=self.alass_fps_guessing.isChecked(),
            disable_speed_optimization=self.alass_speed.isChecked(),
            interval_ms=self.alass_interval.value(),
            allow_negative_timestamps=self.alass_negative.isChecked(),
        )
        sync.lapse = replace(
            sync.lapse,
            mode=self.lapse_mode.currentText(),
            split_penalty=self.lapse_split_penalty.value(),
            confidence=self.lapse_confidence.value(),
            force=self.lapse_force.isChecked(),
            use_embedded=self.lapse_embedded.isChecked(),
            full_scan=self.lapse_full_scan.isChecked(),
            no_cache=self.lapse_no_cache.isChecked(),
        )
        sync.autosubsync = replace(
            sync.autosubsync,
            max_shift_secs=self.autosubsync_max_shift.value(),
            parallelism=self.autosubsync_parallelism.value(),
        )
        updated = AppConfig(
            sync=sync,
            theme=Theme(self.theme.currentData()),
            concurrency=self.concurrency.value(),
            match_threshold=self.match_threshold.value(),
            recursive_scan=self.recursive_scan.isChecked(),
            output_suffix=self.output_suffix.text().strip(),
            speech_cache_enabled=self.speech_cache.isChecked(),
            history_limit=self.history_limit.value(),
            auto_start=self.auto_start.isChecked(),
            # Not editable here, but it is a saved setting: dropping it would
            # reset the match dialog's toggle every time settings are saved.
            fast_mode=self._config.fast_mode,
        )
        return updated.normalized()
