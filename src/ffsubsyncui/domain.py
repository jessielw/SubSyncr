from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self
from uuid import uuid4


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MatchMethod(StrEnum):
    REGEX = "identifier"
    EXACT = "exact name"
    FUZZY = "fuzzy"
    MANUAL = "manual"
    UNMATCHED = "unmatched"


@dataclass(slots=True)
class SyncOptions:
    overwrite_input: bool = False
    replace_output: bool = False
    encoding: str = "infer"
    output_encoding: str = "utf-8"
    vad: str = "subs_then_webrtc"
    max_offset_seconds: float = 60.0
    no_fix_framerate: bool = False
    quality_gate: bool = False
    reference_stream: str = ""
    ffmpeg_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        if not isinstance(value, dict):
            return cls()
        defaults = cls()
        return cls(
            overwrite_input=bool(value.get("overwrite_input", False)),
            replace_output=bool(value.get("replace_output", False)),
            encoding=str(value.get("encoding", defaults.encoding)),
            output_encoding=str(value.get("output_encoding", defaults.output_encoding)),
            vad=str(value.get("vad", defaults.vad)),
            max_offset_seconds=float(
                value.get("max_offset_seconds", defaults.max_offset_seconds)
            ),
            no_fix_framerate=bool(value.get("no_fix_framerate", False)),
            quality_gate=bool(value.get("quality_gate", False)),
            reference_stream=str(value.get("reference_stream", "")),
            ffmpeg_path=str(value.get("ffmpeg_path", "")),
        )


@dataclass(slots=True)
class SyncResult:
    success: bool
    return_code: int
    offset_seconds: float | None = None
    framerate_scale_factor: float | None = None
    error: str = ""


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
        }

    @classmethod
    def from_record(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise ValueError("Queue job must be an object")
        try:
            status = JobStatus(str(value.get("status", JobStatus.QUEUED)))
            interrupted = status is JobStatus.RUNNING
            if interrupted:
                status = JobStatus.QUEUED
            error = str(value.get("last_error", ""))
            if interrupted:
                error = "Interrupted during the previous application session"
            return cls(
                id=str(value["id"]),
                enabled=bool(value.get("enabled", True)),
                status=status,
                reference_path=Path(str(value["reference_path"])),
                subtitle_path=Path(str(value["subtitle_path"])),
                output_path=Path(str(value["output_path"])),
                options=SyncOptions.from_dict(value.get("options")),
                last_error=error,
                stage="Interrupted" if interrupted else "Waiting",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid queue job record") from exc


@dataclass(slots=True, frozen=True)
class MatchProposal:
    reference_path: Path
    subtitle_path: Path | None
    method: MatchMethod
    score: float
    ambiguity_reason: str = ""


def default_output_for(subtitle_path: Path) -> Path:
    return subtitle_path.with_name(f"{subtitle_path.stem}.synced.srt")
