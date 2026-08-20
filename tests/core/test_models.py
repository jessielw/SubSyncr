"""Job and option models, including their persistence round trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffsubsyncui.core.models import (
    JobStatus,
    SyncJob,
    SyncOptions,
    default_output_for,
)


def test_options_round_trip() -> None:
    options = SyncOptions(
        vad="auditok",
        split_penalty=7.5,
        max_duration_seconds=600.0,
        skip_sync_on_low_quality=True,
    )
    assert SyncOptions.from_dict(options.to_dict()) == options


def test_options_survive_a_hand_mangled_file() -> None:
    """A bad config must fall back, never crash the app on startup."""
    restored = SyncOptions.from_dict(
        {"max_offset_seconds": "not a number", "segment_count": None, "vad": "webrtc"}
    )
    assert restored.vad == "webrtc"
    assert restored.max_offset_seconds == SyncOptions().max_offset_seconds
    assert restored.segment_count == SyncOptions().segment_count


def test_options_from_garbage_yields_defaults() -> None:
    assert SyncOptions.from_dict("nonsense") == SyncOptions()


def test_none_is_preserved_for_optional_options() -> None:
    """split_penalty and max_duration mean something different when unset."""
    options = SyncOptions.from_dict({"split_penalty": None})
    assert options.split_penalty is None


def test_speech_cache_key_tracks_extraction_settings() -> None:
    base = SyncOptions()
    assert base.speech_cache_key() == SyncOptions().speech_cache_key()
    # Anything baked into the serialized array must change the key...
    assert base.speech_cache_key() != SyncOptions(vad="auditok").speech_cache_key()
    assert base.speech_cache_key() != SyncOptions(start_seconds=30).speech_cache_key()
    # ...and anything that is not must leave it alone.
    assert base.speech_cache_key() == SyncOptions(encoding="utf-8").speech_cache_key()


@pytest.mark.parametrize(
    ("subtitle", "expected"),
    [
        ("ep.srt", "ep.synced.srt"),
        ("ep.en.srt", "ep.en.synced.srt"),
        ("ep.en.forced.ass", "ep.en.forced.synced.ass"),
        ("ep.vtt", "ep.synced.vtt"),
        # ffsubsync parses .sub but cannot write it.
        ("ep.sub", "ep.synced.srt"),
    ],
)
def test_default_output_preserves_language_and_format(
    subtitle: str, expected: str
) -> None:
    assert default_output_for(Path(subtitle)).name == expected


def test_custom_output_suffix() -> None:
    assert default_output_for(Path("ep.en.srt"), ".fixed").name == "ep.en.fixed.srt"


def test_job_record_round_trip(tmp_path: Path) -> None:
    job = SyncJob(
        reference_path=tmp_path / "ep.mkv",
        subtitle_path=tmp_path / "ep.en.srt",
        output_path=tmp_path / "ep.en.synced.srt",
        language="en",
        group_key="abc123",
    )
    restored = SyncJob.from_record(job.to_record())
    assert restored.id == job.id
    assert restored.reference_path == job.reference_path
    assert restored.language == "en"
    assert restored.group_key == "abc123"


def test_interrupted_job_is_requeued_not_reported_as_running() -> None:
    """A RUNNING row on load means we died mid-job."""
    record = SyncJob(
        reference_path=Path("ep.mkv"),
        subtitle_path=Path("ep.srt"),
        output_path=Path("out.srt"),
        status=JobStatus.RUNNING,
    ).to_record()
    restored = SyncJob.from_record(record)
    assert restored.status is JobStatus.QUEUED
    assert "Interrupted" in restored.last_error


def test_invalid_record_is_rejected() -> None:
    with pytest.raises(ValueError):
        SyncJob.from_record({"id": "x"})


def test_effective_output_follows_overwrite_flag(tmp_path: Path) -> None:
    job = SyncJob(
        reference_path=tmp_path / "ep.mkv",
        subtitle_path=tmp_path / "ep.srt",
        output_path=tmp_path / "out.srt",
    )
    assert job.effective_output == tmp_path / "out.srt"
    job.options.overwrite_input = True
    assert job.effective_output == tmp_path / "ep.srt"


@pytest.mark.parametrize(
    ("status", "terminal", "retryable"),
    [
        (JobStatus.QUEUED, False, False),
        (JobStatus.RUNNING, False, False),
        (JobStatus.SUCCEEDED, True, False),
        (JobStatus.FAILED, True, True),
        (JobStatus.CANCELLED, True, True),
        (JobStatus.SKIPPED_LOW_QUALITY, True, True),
    ],
)
def test_status_classification(
    status: JobStatus, terminal: bool, retryable: bool
) -> None:
    assert status.is_terminal is terminal
    assert status.is_retryable is retryable


def test_retry_clears_previous_outcome(tmp_path: Path) -> None:
    job = SyncJob(
        reference_path=tmp_path / "ep.mkv",
        subtitle_path=tmp_path / "ep.srt",
        output_path=tmp_path / "out.srt",
        status=JobStatus.FAILED,
        last_error="boom",
        progress=0.5,
    )
    job.reset_for_retry()
    assert job.status is JobStatus.QUEUED
    assert job.last_error == ""
    assert job.progress is None
    assert job.result is None
