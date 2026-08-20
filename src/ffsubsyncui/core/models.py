"""Dataclasses shared by every frontend. No Qt, no I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
import time
from typing import Any, Self
from uuid import uuid4

# ffsubsync only knows how to *write* these (generic_subtitles.write_file).
# ".sub" parses on input but raises NotImplementedError on output.
WRITABLE_SUFFIXES = frozenset({".srt", ".ass", ".ssa", ".vtt"})

# ffsubsync constants.VAD_CHOICES, minus "silero" and the "fused*" strategies,
# which need torch. We deliberately exclude torch from the bundle.
VAD_CHOICES = (
    "subs_then_webrtc",
    "webrtc",
    "subs_then_auditok",
    "auditok",
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # --skip-sync-on-low-quality tripped: ffsubsync copied the *original* subs
    # to the output path rather than apply a probably-wrong shift. Not a
    # failure, but emphatically not a success either.
    SKIPPED_LOW_QUALITY = "skipped_low_quality"

    @property
    def is_terminal(self) -> bool:
        return self is not JobStatus.QUEUED and self is not JobStatus.RUNNING

    @property
    def is_retryable(self) -> bool:
        return self in (
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.SKIPPED_LOW_QUALITY,
        )


class MatchMethod(StrEnum):
    IDENTIFIER = "identifier"
    EXACT = "exact"
    FUZZY = "fuzzy"
    MANUAL = "manual"
    UNMATCHED = "unmatched"


def _as_bool(value: object, default: bool) -> bool:
    return bool(value) if value is not None else default


def _as_str(value: object, default: str) -> str:
    return str(value) if value is not None else default


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class SyncOptions:
    """Every ffsubsync knob we expose, in the units ffsubsync itself uses."""

    # Alignment
    vad: str = "subs_then_webrtc"
    max_offset_seconds: float = 60.0
    no_fix_framerate: bool = False
    gss: bool = False
    reference_stream: str = ""
    # Piecewise / segmented alignment
    split_penalty: float | None = None
    multi_segment_sync: bool = False
    segment_count: int = 8
    max_duration_seconds: float | None = None
    start_seconds: int = 0
    # Quality gate
    skip_sync_on_low_quality: bool = False
    min_score: float = 0.0
    quality_max_offset_seconds: float = 30.0
    max_framerate_deviation: float = 0.1
    # Input / output
    encoding: str = "infer"
    output_encoding: str = "utf-8"
    overwrite_input: bool = False
    replace_output: bool = False
    # Only meaningful with overwrite_input: keep a copy of the untouched
    # subtitle before ffsubsync writes over it.
    backup_before_overwrite: bool = True
    apply_offset_seconds: float = 0.0
    # Environment
    ffmpeg_path: str = ""
    frame_rate: int = 48000
    non_speech_label: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            return cls()
        d = cls()
        return cls(
            vad=_as_str(value.get("vad"), d.vad),
            max_offset_seconds=_as_float(
                value.get("max_offset_seconds"), d.max_offset_seconds
            ),
            no_fix_framerate=_as_bool(
                value.get("no_fix_framerate"), d.no_fix_framerate
            ),
            gss=_as_bool(value.get("gss"), d.gss),
            reference_stream=_as_str(value.get("reference_stream"), d.reference_stream),
            split_penalty=_as_opt_float(value.get("split_penalty")),
            multi_segment_sync=_as_bool(
                value.get("multi_segment_sync"), d.multi_segment_sync
            ),
            segment_count=_as_int(value.get("segment_count"), d.segment_count),
            max_duration_seconds=_as_opt_float(value.get("max_duration_seconds")),
            start_seconds=_as_int(value.get("start_seconds"), d.start_seconds),
            skip_sync_on_low_quality=_as_bool(
                value.get("skip_sync_on_low_quality"), d.skip_sync_on_low_quality
            ),
            min_score=_as_float(value.get("min_score"), d.min_score),
            quality_max_offset_seconds=_as_float(
                value.get("quality_max_offset_seconds"), d.quality_max_offset_seconds
            ),
            max_framerate_deviation=_as_float(
                value.get("max_framerate_deviation"), d.max_framerate_deviation
            ),
            encoding=_as_str(value.get("encoding"), d.encoding),
            output_encoding=_as_str(value.get("output_encoding"), d.output_encoding),
            overwrite_input=_as_bool(value.get("overwrite_input"), d.overwrite_input),
            replace_output=_as_bool(value.get("replace_output"), d.replace_output),
            backup_before_overwrite=_as_bool(
                value.get("backup_before_overwrite"), d.backup_before_overwrite
            ),
            apply_offset_seconds=_as_float(
                value.get("apply_offset_seconds"), d.apply_offset_seconds
            ),
            ffmpeg_path=_as_str(value.get("ffmpeg_path"), d.ffmpeg_path),
            frame_rate=_as_int(value.get("frame_rate"), d.frame_rate),
            non_speech_label=_as_float(
                value.get("non_speech_label"), d.non_speech_label
            ),
        )

    @property
    def samples_reference(self) -> bool:
        """Whether this reads short segments instead of the whole reference."""
        return self.multi_segment_sync

    def speech_cache_key(
        self,
    ) -> tuple[str, int, int, float, float | None, bool, int]:
        """Options baked into a serialized ``.npz`` speech array.

        Reusing a cached array under different values for any of these would
        silently align against the wrong reference, so they key the cache.
        """
        return (
            self.vad,
            self.start_seconds,
            self.frame_rate,
            self.non_speech_label,
            self.max_duration_seconds,
            self.multi_segment_sync,
            self.segment_count,
        )


@dataclass(slots=True)
class SyncResult:
    status: JobStatus
    return_code: int = 0
    offset_seconds: float | None = None
    framerate_scale_factor: float | None = None
    # ffsubsync never returns its alignment score; the worker scrapes it out of
    # the "score: %.3f" log record. Magnitude is unnormalized, sign is meaningful.
    score: float | None = None
    quality_reasons: list[str] = field(default_factory=list)
    error: str = ""
    duration_seconds: float | None = None
    # The reference was a speech array cached by an earlier session.
    used_speech_cache: bool = False
    # A sibling subtitle in the same group paid for the extraction this job
    # reused, in memory, within one worker process.
    shared_extraction: bool = False

    @property
    def success(self) -> bool:
        return self.status is JobStatus.SUCCEEDED

    @property
    def skipped_extraction(self) -> bool:
        return self.used_speech_cache or self.shared_extraction


@dataclass(frozen=True, slots=True)
class MatchResult:
    """A proposed video/subtitle pairing, with its reasoning attached."""

    video: Path
    subtitle: Path | None
    confidence: float
    method: MatchMethod
    language: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.subtitle is not None


@dataclass(slots=True)
class SyncJob:
    reference_path: Path
    subtitle_path: Path
    output_path: Path
    options: SyncOptions = field(default_factory=SyncOptions)
    id: str = field(default_factory=lambda: str(uuid4()))
    enabled: bool = True
    status: JobStatus = JobStatus.QUEUED
    progress: float | None = None
    stage: str = "Waiting"
    result: SyncResult | None = None
    last_error: str = ""
    language: str | None = None
    # Jobs sharing a group key share a reference file, so the first to run can
    # serialize its speech array and the rest reuse it. See core.speech_cache.
    group_key: str | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def effective_output(self) -> Path:
        """Where the synced subtitles actually land."""
        return self.subtitle_path if self.options.overwrite_input else self.output_path

    def reset_for_retry(self) -> None:
        self.status = JobStatus.QUEUED
        self.progress = None
        self.stage = "Waiting"
        self.last_error = ""
        self.result = None

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "status": self.status.value,
            "reference_path": str(self.reference_path),
            "subtitle_path": str(self.subtitle_path),
            "output_path": str(self.output_path),
            "options": self.options.to_dict(),
            "last_error": self.last_error,
            "language": self.language,
            "group_key": self.group_key,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            raise ValueError("Queue job must be a mapping")
        try:
            status = JobStatus(str(value.get("status", JobStatus.QUEUED)))
        except ValueError:
            status = JobStatus.QUEUED
        # A RUNNING row means we died mid-job; requeue it rather than lie.
        interrupted = status is JobStatus.RUNNING
        if interrupted:
            status = JobStatus.QUEUED
        try:
            return cls(
                id=str(value["id"]),
                enabled=_as_bool(value.get("enabled"), True),
                status=status,
                reference_path=Path(str(value["reference_path"])),
                subtitle_path=Path(str(value["subtitle_path"])),
                output_path=Path(str(value["output_path"])),
                options=SyncOptions.from_dict(value.get("options")),
                last_error=(
                    "Interrupted during the previous session"
                    if interrupted
                    else _as_str(value.get("last_error"), "")
                ),
                stage="Interrupted" if interrupted else "Waiting",
                language=(
                    str(value["language"])
                    if value.get("language") is not None
                    else None
                ),
                group_key=(
                    str(value["group_key"])
                    if value.get("group_key") is not None
                    else None
                ),
                created_at=_as_float(value.get("created_at"), time.time()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid queue job record") from exc


def default_output_for(subtitle_path: Path, suffix: str = ".synced") -> Path:
    """Sibling output path, preserving any language tag in the stem.

    ``ep.en.srt`` becomes ``ep.en.synced.srt``. ``.sub`` inputs are redirected
    to ``.srt`` because ffsubsync cannot serialize the ``.sub`` format.
    """
    extension = subtitle_path.suffix.lower()
    if extension not in WRITABLE_SUFFIXES:
        extension = ".srt"
    return subtitle_path.with_name(f"{subtitle_path.stem}{suffix}{extension}")
