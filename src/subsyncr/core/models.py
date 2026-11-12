"""Dataclasses shared by every frontend. No Qt, no I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
import time
from typing import Any, Self
from uuid import uuid4

from subsyncr.core.engines.base import EngineId

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

# lapse's alignment strategies, chosen as a positional subcommand.
LAPSE_MODES = ("auto", "split", "nosplit", "ols")


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # --skip-sync-on-low-quality tripped: ffsubsync copied the *original* subs
    # to the output path rather than apply a probably-wrong shift. Not a
    # failure, but emphatically not a success either. lapse's exit code 3 (an
    # "unsure" verdict) maps here too.
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


class OutputPolicy(StrEnum):
    """Where a synced subtitle is written.

    The default puts it beside the original under a suffixed name, which is the
    only option that cannot lose anything. The rest exist because a media
    library usually wants the file somewhere specific - next to the video, or
    named after it, so a player finds it without being told.
    """

    # ep.en.srt -> ep.en.synced.srt, in the subtitle's own folder.
    BESIDE_SUBTITLE = "beside_subtitle"
    # Write over the input. Backed up first unless that is turned off.
    OVERWRITE = "overwrite"
    # The subtitle's name, in the video's folder.
    BESIDE_VIDEO = "beside_video"
    # The video's name with the subtitle's extension, in the video's folder -
    # what players look for when they auto-load a sidecar.
    VIDEO_NAME = "video_name"
    DESKTOP = "desktop"
    # A folder the user picked; see FileOptions.output_folder.
    FOLDER = "folder"

    @classmethod
    def parse(cls, value: object) -> OutputPolicy:
        try:
            return cls(str(value))
        except ValueError:
            return cls.BESIDE_SUBTITLE


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


def _mapping(value: object) -> Mapping[str, Any]:
    """A mapping to read option fields from, whatever we were handed."""
    return value if isinstance(value, Mapping) else {}


@dataclass(slots=True)
class FileOptions:
    """How inputs are read and where the result is written.

    Every engine honours these, because we apply them ourselves rather than
    delegating to the engine.
    """

    encoding: str = "infer"
    output_encoding: str = "utf-8"
    output_policy: OutputPolicy = OutputPolicy.BESIDE_SUBTITLE
    # Only consulted when output_policy is FOLDER.
    output_folder: str = ""
    replace_output: bool = False
    # Only meaningful when overwriting: keep a copy of the untouched subtitle
    # before the engine writes over it.
    backup_before_overwrite: bool = True

    @property
    def overwrite_input(self) -> bool:
        """Whether the result replaces the file it was made from.

        A view of ``output_policy`` rather than a setting of its own, so the two
        can never disagree. Writable, because "just overwrite this one" is a
        per-job decision several callers make.
        """
        return self.output_policy is OutputPolicy.OVERWRITE

    @overwrite_input.setter
    def overwrite_input(self, value: bool) -> None:
        if value:
            self.output_policy = OutputPolicy.OVERWRITE
        elif self.output_policy is OutputPolicy.OVERWRITE:
            self.output_policy = OutputPolicy.BESIDE_SUBTITLE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        source = _mapping(value)
        d = cls()
        policy = d.output_policy
        if "output_policy" in source:
            policy = OutputPolicy.parse(source.get("output_policy"))
        elif source.get("overwrite_input"):
            # Written before there were policies, when overwriting was a flag.
            policy = OutputPolicy.OVERWRITE
        return cls(
            encoding=_as_str(source.get("encoding"), d.encoding),
            output_encoding=_as_str(source.get("output_encoding"), d.output_encoding),
            output_policy=policy,
            output_folder=_as_str(source.get("output_folder"), d.output_folder),
            replace_output=_as_bool(source.get("replace_output"), d.replace_output),
            backup_before_overwrite=_as_bool(
                source.get("backup_before_overwrite"), d.backup_before_overwrite
            ),
        )


@dataclass(slots=True)
class EnvironmentOptions:
    """Where the external tools every engine leans on actually live."""

    ffmpeg_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        source = _mapping(value)
        d = cls()
        return cls(ffmpeg_path=_as_str(source.get("ffmpeg_path"), d.ffmpeg_path))


@dataclass(slots=True)
class FFSubSyncOptions:
    """Every ffsubsync knob we expose, in the units ffsubsync itself uses."""

    # Alignment
    vad: str = "subs_then_webrtc"
    max_offset_seconds: float = 60.0
    no_fix_framerate: bool = False
    gss: bool = False
    reference_stream: str = ""
    # Align against the video's PGS (image) subtitle track instead of its audio.
    # Empty means off. "auto" lets the worker pick a track by language, and
    # falls back to the audio when the video has none - so it is a safe default:
    # reading PGS timings costs one pass over the file, with no audio decoding.
    pgs_ref_stream: str = "auto"
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
    # Post-processing
    apply_offset_seconds: float = 0.0
    # Reference decoding
    frame_rate: int = 48000
    non_speech_label: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        source = _mapping(value)
        d = cls()
        return cls(
            vad=_as_str(source.get("vad"), d.vad),
            max_offset_seconds=_as_float(
                source.get("max_offset_seconds"), d.max_offset_seconds
            ),
            no_fix_framerate=_as_bool(
                source.get("no_fix_framerate"), d.no_fix_framerate
            ),
            gss=_as_bool(source.get("gss"), d.gss),
            reference_stream=_as_str(
                source.get("reference_stream"), d.reference_stream
            ),
            pgs_ref_stream=_as_str(source.get("pgs_ref_stream"), d.pgs_ref_stream),
            split_penalty=_as_opt_float(source.get("split_penalty")),
            multi_segment_sync=_as_bool(
                source.get("multi_segment_sync"), d.multi_segment_sync
            ),
            segment_count=_as_int(source.get("segment_count"), d.segment_count),
            max_duration_seconds=_as_opt_float(source.get("max_duration_seconds")),
            start_seconds=_as_int(source.get("start_seconds"), d.start_seconds),
            skip_sync_on_low_quality=_as_bool(
                source.get("skip_sync_on_low_quality"), d.skip_sync_on_low_quality
            ),
            min_score=_as_float(source.get("min_score"), d.min_score),
            quality_max_offset_seconds=_as_float(
                source.get("quality_max_offset_seconds"), d.quality_max_offset_seconds
            ),
            max_framerate_deviation=_as_float(
                source.get("max_framerate_deviation"), d.max_framerate_deviation
            ),
            apply_offset_seconds=_as_float(
                source.get("apply_offset_seconds"), d.apply_offset_seconds
            ),
            frame_rate=_as_int(source.get("frame_rate"), d.frame_rate),
            non_speech_label=_as_float(
                source.get("non_speech_label"), d.non_speech_label
            ),
        )

    @property
    def samples_reference(self) -> bool:
        """Whether this reads short segments instead of the whole reference."""
        return self.multi_segment_sync

    def speech_cache_key(
        self,
    ) -> tuple[str, int, int, float, float | None, bool, int, str, str]:
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
            # Which stream the array was built from: PGS timings and audio
            # speech from the same file are entirely different arrays.
            self.reference_stream,
            self.pgs_ref_stream,
        )


@dataclass(slots=True)
class AlassOptions:
    """alass-cli's flags, as its own --help documents them.

    Its split penalty runs 0 to 1000 on a scale of its own - nothing like
    ffsubsync's - so the two are deliberately separate settings.
    """

    # None means --no-split: forbid cutting the subtitle into segments at all.
    split_penalty: float | None = 7.0
    disable_fps_guessing: bool = False
    # alass trades accuracy for speed by default; this turns that off.
    disable_speed_optimization: bool = False
    # Smallest time interval alass will resolve. Smaller is more accurate and
    # slower.
    interval_ms: int = 1
    # alass clamps a negative result to zero unless this is set, because
    # negative timestamps break some players.
    allow_negative_timestamps: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        source = _mapping(value)
        d = cls()
        return cls(
            split_penalty=(
                _as_opt_float(source.get("split_penalty"))
                if "split_penalty" in source
                else d.split_penalty
            ),
            disable_fps_guessing=_as_bool(
                source.get("disable_fps_guessing"), d.disable_fps_guessing
            ),
            disable_speed_optimization=_as_bool(
                source.get("disable_speed_optimization"), d.disable_speed_optimization
            ),
            interval_ms=max(1, _as_int(source.get("interval_ms"), d.interval_ms)),
            allow_negative_timestamps=_as_bool(
                source.get("allow_negative_timestamps"), d.allow_negative_timestamps
            ),
        )


@dataclass(slots=True)
class LapseOptions:
    """lapse's flags. Mode is a positional subcommand, not a switch."""

    mode: str = "auto"
    split_penalty: float = 6.0
    # lapse finds and uses embedded subtitle tracks itself; turning this off
    # passes --no-embedded and forces it onto the audio.
    use_embedded: bool = True
    full_scan: bool = False
    # lapse keeps its own on-disk analysis cache; this bypasses it.
    no_cache: bool = False
    # How far lapse's answer must stand out before it will write. Its own
    # default; lower accepts more alignments, higher refuses more.
    confidence: float = 8.0
    # Apply the shift even when lapse says it is unsure. Off by default, so an
    # uncertain result leaves the original alone - the same bargain ffsubsync's
    # quality gate offers.
    force: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        source = _mapping(value)
        d = cls()
        mode = _as_str(source.get("mode"), d.mode)
        return cls(
            mode=mode if mode in LAPSE_MODES else d.mode,
            split_penalty=_as_float(source.get("split_penalty"), d.split_penalty),
            use_embedded=_as_bool(source.get("use_embedded"), d.use_embedded),
            full_scan=_as_bool(source.get("full_scan"), d.full_scan),
            no_cache=_as_bool(source.get("no_cache"), d.no_cache),
            confidence=_as_float(source.get("confidence"), d.confidence),
            force=_as_bool(source.get("force"), d.force),
        )


@dataclass(slots=True)
class AutoSubSyncOptions:
    """autosubsync's search parameters, passed to ``synchronize()`` as kwargs."""

    max_shift_secs: float = 20.0
    # Worker processes autosubsync forks for feature extraction. The queue
    # already runs several jobs at once, so this multiplies with concurrency.
    parallelism: int = 3
    # Skip skew detection and assume this playback-rate ratio. None means
    # detect it, which is almost always what you want.
    fixed_skew: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        source = _mapping(value)
        d = cls()
        return cls(
            max_shift_secs=_as_float(source.get("max_shift_secs"), d.max_shift_secs),
            parallelism=max(1, _as_int(source.get("parallelism"), d.parallelism)),
            fixed_skew=_as_opt_float(source.get("fixed_skew")),
        )


# Top-level keys that only ever appeared in the pre-multi-engine flat layout.
_LEGACY_MARKERS = frozenset(
    {"vad", "max_offset_seconds", "gss", "overwrite_input", "no_fix_framerate"}
)


@dataclass(slots=True)
class SyncOptions:
    """The full option surface: what to run, and how, for every engine.

    Each engine's settings are kept simultaneously rather than being replaced
    when the engine changes, so switching back and forth never loses a
    configuration the user spent time on.
    """

    engine: EngineId = EngineId.FFSUBSYNC
    # Engines to retry with, in order, when the chosen one fails or returns a
    # low-quality verdict. Empty means a failure is final.
    fallback_chain: tuple[EngineId, ...] = ()
    files: FileOptions = field(default_factory=FileOptions)
    environment: EnvironmentOptions = field(default_factory=EnvironmentOptions)
    # Pull an embedded subtitle track out of the video and align against that
    # instead of its audio. Far cheaper than speech detection when it works.
    extract_embedded: bool = False
    ffsubsync: FFSubSyncOptions = field(default_factory=FFSubSyncOptions)
    alass: AlassOptions = field(default_factory=AlassOptions)
    lapse: LapseOptions = field(default_factory=LapseOptions)
    autosubsync: AutoSubSyncOptions = field(default_factory=AutoSubSyncOptions)

    def for_engine(
        self, engine: EngineId | None = None
    ) -> FFSubSyncOptions | AlassOptions | LapseOptions | AutoSubSyncOptions:
        """The settings block belonging to one engine, defaulting to the active one."""
        match engine or self.engine:
            case EngineId.ALASS:
                return self.alass
            case EngineId.LAPSE:
                return self.lapse
            case EngineId.AUTOSUBSYNC:
                return self.autosubsync
            case _:
                return self.ffsubsync

    def copy(self) -> Self:
        """An independent copy, nested option blocks included.

        ``dataclasses.replace`` is shallow, which would leave every job built
        from one template sharing a single ``FileOptions`` instance - so setting
        ``overwrite_input`` on one job would set it on all of them.
        """
        return type(self)(
            engine=self.engine,
            fallback_chain=self.fallback_chain,
            files=replace(self.files),
            environment=replace(self.environment),
            extract_embedded=self.extract_embedded,
            ffsubsync=replace(self.ffsubsync),
            alass=replace(self.alass),
            lapse=replace(self.lapse),
            autosubsync=replace(self.autosubsync),
        )

    def with_engine(self, engine: EngineId) -> Self:
        """A copy that runs ``engine`` first, keeping the rest of the chain.

        The engine is dropped from the chain so a failure does not simply run
        the same engine again.
        """
        options = self.copy()
        options.engine = engine
        options.fallback_chain = tuple(
            item for item in self.fallback_chain if item is not engine
        )
        return options

    def identity(self) -> dict[str, Any]:
        """The subset that decides whether two jobs can share a worker process.

        Another engine's settings are irrelevant to this run, so they are left
        out - otherwise editing alass options would split ffsubsync groups.
        """
        return {
            "engine": self.engine.value,
            "files": self.files.to_dict(),
            "environment": self.environment.to_dict(),
            "extract_embedded": self.extract_embedded,
            "options": self.for_engine().to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine.value,
            "fallback_chain": [engine.value for engine in self.fallback_chain],
            "files": self.files.to_dict(),
            "environment": self.environment.to_dict(),
            "extract_embedded": self.extract_embedded,
            "ffsubsync": self.ffsubsync.to_dict(),
            "alass": self.alass.to_dict(),
            "lapse": self.lapse.to_dict(),
            "autosubsync": self.autosubsync.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            return cls()
        if "engine" not in value and _LEGACY_MARKERS & set(value):
            # Pre-multi-engine config: one flat block of ffsubsync's knobs mixed
            # with file and environment settings. Every reader picks its own
            # keys out of the same mapping.
            return cls(
                engine=EngineId.FFSUBSYNC,
                files=FileOptions.from_dict(value),
                environment=EnvironmentOptions.from_dict(value),
                ffsubsync=FFSubSyncOptions.from_dict(value),
            )
        default = cls()
        return cls(
            engine=EngineId.parse(value.get("engine"), default.engine),
            fallback_chain=_engine_chain(value.get("fallback_chain")),
            files=FileOptions.from_dict(value.get("files")),
            environment=EnvironmentOptions.from_dict(value.get("environment")),
            extract_embedded=_as_bool(
                value.get("extract_embedded"), default.extract_embedded
            ),
            ffsubsync=FFSubSyncOptions.from_dict(value.get("ffsubsync")),
            alass=AlassOptions.from_dict(value.get("alass")),
            lapse=LapseOptions.from_dict(value.get("lapse")),
            autosubsync=AutoSubSyncOptions.from_dict(value.get("autosubsync")),
        )


def _engine_chain(value: object) -> tuple[EngineId, ...]:
    """Parse a fallback chain, dropping unknowns and duplicates."""
    if not isinstance(value, (list, tuple)):
        return ()
    seen: list[EngineId] = []
    for item in value:
        try:
            engine = EngineId(str(item))
        except ValueError:
            continue
        if engine not in seen:
            seen.append(engine)
    return tuple(seen)


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
    # Which engine actually produced this. Not always the job's configured
    # engine, once a fallback chain is in play.
    engine: EngineId | None = None

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


class ReferenceKind(StrEnum):
    """What part of a video reference a job aligns against."""

    AUDIO = "audio"
    # An image subtitle track's display timings (ffsubsync --pgs-ref-stream).
    PGS = "pgs"
    # A text subtitle track, extracted and used in place of the video.
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ReferenceTrack:
    """The exact source a job syncs against, settled before it runs.

    Resolving "auto" when the job is queued rather than inside the worker means
    the queue can show it, the user can change it, and the speech cache is
    keyed by the track actually used rather than by the word "auto".
    """

    kind: ReferenceKind
    # Position among the container's subtitle streams (ffmpeg's 0:s:N).
    index: int | None = None
    language: str = ""
    title: str = ""
    # Picked by the user rather than by the automatic rules.
    chosen: bool = False

    @classmethod
    def audio(cls, *, chosen: bool = False) -> Self:
        return cls(ReferenceKind.AUDIO, chosen=chosen)

    @property
    def token(self) -> str:
        """Identity for grouping and caching: kind and stream, nothing cosmetic."""
        if self.kind is ReferenceKind.AUDIO or self.index is None:
            return ReferenceKind.AUDIO.value
        return f"{self.kind.value}:{self.index}"

    def describe(self) -> str:
        """Short label for a table cell, e.g. "PGS · eng · Signs & Songs"."""
        if self.kind is ReferenceKind.AUDIO:
            return "Audio"
        parts = ["PGS" if self.kind is ReferenceKind.PGS else "Text"]
        parts.extend(part for part in (self.language, self.title) if part)
        if len(parts) == 1:
            parts.append(f"s:{self.index}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "index": self.index,
            "language": self.language,
            "title": self.title,
            "chosen": self.chosen,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self | None:
        if not isinstance(value, Mapping):
            return None
        try:
            kind = ReferenceKind(str(value.get("kind")))
        except ValueError:
            return None
        raw_index = value.get("index")
        index = raw_index if isinstance(raw_index, int) and raw_index >= 0 else None
        if kind is not ReferenceKind.AUDIO and index is None:
            return None
        return cls(
            kind=kind,
            index=index if kind is not ReferenceKind.AUDIO else None,
            language=_as_str(value.get("language"), ""),
            title=_as_str(value.get("title"), ""),
            chosen=_as_bool(value.get("chosen"), False),
        )


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
    # None until the queue has probed the reference; see core.reference.
    reference_track: ReferenceTrack | None = None

    @property
    def effective_output(self) -> Path:
        """Where the synced subtitles actually land."""
        return (
            self.subtitle_path
            if self.options.files.overwrite_input
            else self.output_path
        )

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
            "reference_track": (
                self.reference_track.to_dict() if self.reference_track else None
            ),
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
                reference_track=ReferenceTrack.from_dict(value.get("reference_track")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid queue job record") from exc


def resolve_output(
    video: Path,
    subtitle: Path,
    files: FileOptions,
    suffix: str = ".synced",
) -> Path:
    """Where this job's result should be written, per the output policy."""
    extension = subtitle.suffix.lower()
    if extension not in WRITABLE_SUFFIXES:
        extension = ".srt"
    match files.output_policy:
        case OutputPolicy.OVERWRITE:
            return subtitle
        case OutputPolicy.BESIDE_VIDEO:
            return video.parent / f"{subtitle.stem}{suffix}{extension}"
        case OutputPolicy.VIDEO_NAME:
            # Keep language and accessibility variants distinct while still
            # using the video's basename for automatic player discovery.
            from subsyncr.core.language import parse_subtitle_tags

            tags = parse_subtitle_tags(subtitle)
            parts = [video.stem]
            if tags.language:
                parts.append(tags.language)
            if tags.forced:
                parts.append("forced")
            if tags.sdh:
                parts.append("sdh")
            return video.parent / (".".join(parts) + extension)
        case OutputPolicy.DESKTOP:
            return _desktop() / f"{subtitle.stem}{suffix}{extension}"
        case OutputPolicy.FOLDER:
            folder = Path(files.output_folder).expanduser()
            if folder.is_dir():
                return folder / f"{subtitle.stem}{suffix}{extension}"
            # A folder that has gone away must not send the file somewhere
            # surprising; fall back to the safe default.
            return default_output_for(subtitle, suffix)
        case _:
            return default_output_for(subtitle, suffix)


def _desktop() -> Path:
    from platformdirs import user_desktop_path

    try:
        desktop = Path(user_desktop_path())
    except Exception:
        return Path.home()
    return desktop if desktop.is_dir() else Path.home()


def default_output_for(subtitle_path: Path, suffix: str = ".synced") -> Path:
    """Sibling output path, preserving any language tag in the stem.

    ``ep.en.srt`` becomes ``ep.en.synced.srt``. ``.sub`` inputs are redirected
    to ``.srt`` because ffsubsync cannot serialize the ``.sub`` format.
    """
    extension = subtitle_path.suffix.lower()
    if extension not in WRITABLE_SUFFIXES:
        extension = ".srt"
    return subtitle_path.with_name(f"{subtitle_path.stem}{suffix}{extension}")
