"""Single-job dialog, for the one-off pairing that does not need a folder scan."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from subsyncr.core.engine import first_capable, routing_notice
from subsyncr.core.engines import EngineId
from subsyncr.core.language import parse_subtitle_tags
from subsyncr.core.models import SyncJob, SyncOptions, default_output_for
from subsyncr.core.planner import build_job
from subsyncr.core.scanner import SUBTITLE_REFERENCE_EXTENSIONS
from subsyncr.qt.sizing import SIZE_ADD_JOB, SPACING
from subsyncr.qt.widgets import MEDIA_FILTER, SUBTITLE_FILTER, EngineCombo, PathField

# The .sub format is read but never written back: resolve_output redirects it,
# whichever engine runs.
_SUB_FORMAT_NOTE = "The .sub format cannot be written; the output will be .srt."


class AddJobDialog(QDialog):
    def __init__(
        self,
        options: SyncOptions,
        *,
        output_suffix: str = ".synced",
        fast_mode: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add job")
        self.resize(*SIZE_ADD_JOB)
        self._options = options
        self._output_suffix = output_suffix
        self._output_edited = False

        self.engine = EngineCombo(options.engine, self)
        self.engine.setToolTip(
            "The engine this job runs with. The fallback chain from Settings "
            "still applies if it fails."
        )
        self.engine.currentIndexChanged.connect(self._validate)

        self.reference = PathField(
            file_filter=MEDIA_FILTER,
            caption="Select the reference",
            on_change=self._validate,
            parent=self,
        )
        self.subtitle = PathField(
            file_filter=SUBTITLE_FILTER,
            caption="Select the subtitle",
            on_change=self._subtitle_changed,
            parent=self,
        )
        self.output = QLineEdit(self)
        self.output.textEdited.connect(self._mark_output_edited)
        self.overwrite = QCheckBox("Write over the input subtitle", self)
        self.overwrite.toggled.connect(self._overwrite_toggled)
        self.backup = QCheckBox("Keep a .bak copy of the original", self)
        self.backup.setChecked(options.files.backup_before_overwrite)
        self.backup.setEnabled(False)
        self.overwrite.toggled.connect(self.backup.setEnabled)
        self.replace = QCheckBox("Allow replacing an existing output", self)
        self.replace.setChecked(options.files.replace_output)
        self.fast_mode = QCheckBox(
            "Fast mode - sample the audio instead of reading the whole file", self
        )
        self.fast_mode.setChecked(fast_mode)
        self.fast_mode.setToolTip(
            "Samples short audio segments when ffsubsync runs, including as a "
            "fallback. Adjust the segment count in Settings > ffsubsync."
        )
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)

        form = QFormLayout()
        form.addRow("Reference", self.reference)
        form.addRow("Subtitle", self.subtitle)
        form.addRow("Engine", self.engine)
        form.addRow("Output", self.output)
        form.addRow("", self.overwrite)
        form.addRow("", self.backup)
        form.addRow("", self.replace)
        form.addRow("", self.fast_mode)

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING)
        layout.addLayout(form)
        layout.addWidget(self.hint)
        layout.addStretch(1)
        layout.addWidget(buttons)
        self._validate()

    @Slot()
    def _mark_output_edited(self) -> None:
        self._output_edited = True

    @Slot()
    def _subtitle_changed(self) -> None:
        # Keep the suggested output in step with the subtitle until the user
        # takes it over.
        if not self._output_edited and self.subtitle.text():
            suggested = default_output_for(
                Path(self.subtitle.text()), self._output_suffix
            )
            self.output.setText(str(suggested))
        self._validate()

    @Slot(bool)
    def _overwrite_toggled(self, checked: bool) -> None:
        self.output.setEnabled(not checked)
        self._validate()

    def _options_for_engine(self) -> SyncOptions:
        return self._options.with_engine(self.engine.engine())

    def _fast_mode_available(self) -> bool:
        options = self._options_for_engine()
        return (
            options.engine == EngineId.FFSUBSYNC
            or EngineId.FFSUBSYNC in options.fallback_chain
        )

    @Slot()
    def _validate(self) -> None:
        reference = Path(self.reference.text()) if self.reference.text() else None
        subtitle = Path(self.subtitle.text()) if self.subtitle.text() else None
        self.fast_mode.setEnabled(
            self._fast_mode_available()
            and (
                reference is None
                or reference.suffix.lower() not in SUBTITLE_REFERENCE_EXTENSIONS
            )
        )
        problems: list[str] = []
        # Advice, not obstacles: the job still runs.
        notes: list[str] = []
        if reference is None or not reference.is_file():
            problems.append("Choose an existing reference file.")
        if subtitle is None or not subtitle.is_file():
            problems.append("Choose an existing subtitle file.")
        if not self.overwrite.isChecked() and not self.output.text().strip():
            problems.append("Choose an output path.")
        if subtitle is not None and subtitle.suffix.casefold() == ".sub":
            # It just lands as .srt.
            notes.append(_SUB_FORMAT_NOTE)
        if reference is not None and subtitle is not None:
            # Only the file types matter here, so no output or group is worked
            # out - both touch the disk, and the path may still be half-typed.
            probe = SyncJob(
                reference_path=reference,
                subtitle_path=subtitle,
                output_path=subtitle,
                options=self._options_for_engine(),
            )
            if notice := routing_notice(probe):
                target = problems if first_capable(probe) is None else notes
                target.append(notice)
        self.hint.setText(" ".join(problems + notes))
        self._ok.setEnabled(not problems)

    def job(self) -> SyncJob:
        reference = Path(self.reference.text())
        subtitle = Path(self.subtitle.text())
        job = build_job(
            reference,
            subtitle,
            self._options_for_engine(),
            output_suffix=self._output_suffix,
            language=parse_subtitle_tags(subtitle).label or None,
        )
        if self.output.text().strip():
            job.output_path = Path(self.output.text().strip())
        job.options.files.overwrite_input = self.overwrite.isChecked()
        job.options.files.backup_before_overwrite = self.backup.isChecked()
        job.options.files.replace_output = self.replace.isChecked()
        job.options.ffsubsync.multi_segment_sync = (
            self.fast_mode.isEnabled() and self.fast_mode.isChecked()
        )
        return job
