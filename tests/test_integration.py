"""End-to-end tests that spawn the real ffsubsync worker.

Subtitle files are used as the *reference* throughout, which is a first-class
ffsubsync mode and needs no ffmpeg, so these run anywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
import time

import pytest
from support import sync_options

from subsyncr.core.engine import SyncProcess, build_spec, validate
from subsyncr.core.models import (
    JobStatus,
    SyncJob,
    SyncResult,
    default_output_for,
)

pytestmark = pytest.mark.slow


def _timestamp(seconds: float) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    milliseconds = round((secs % 1) * 1000)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d},{milliseconds:03d}"


def _write_subtitles(path: Path, starts: list[float], shift: float = 0.0) -> Path:
    blocks = [
        f"{index}\n"
        f"{_timestamp(start + shift)} --> {_timestamp(start + shift + 1.4)}\n"
        f"Line {index}\n"
        for index, start in enumerate(starts, 1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


@pytest.fixture
def cue_times() -> list[float]:
    """Irregular spacing, so an offset cannot be confused with its negative."""
    starts: list[float] = []
    position = 4.0
    for step in (3.1, 5.7, 2.3, 8.2, 4.4, 6.9, 3.5, 7.1, 2.8, 5.2) * 5:
        starts.append(position)
        position += step
    return starts


@pytest.fixture
def make_pair(
    tmp_path: Path, cue_times: list[float]
) -> Callable[[float], tuple[Path, Path]]:
    def build(shift: float) -> tuple[Path, Path]:
        reference = _write_subtitles(tmp_path / "reference.srt", cue_times)
        drifted = _write_subtitles(tmp_path / "input.srt", cue_times, shift=shift)
        return reference, drifted

    return build


def _run_one(job: SyncJob) -> SyncResult:
    """Run a single-job group and unwrap its one result."""
    return SyncProcess(build_spec([job])).run()[job.id]


def _job(reference: Path, subtitle: Path, **options: object) -> SyncJob:
    return SyncJob(
        reference_path=reference,
        subtitle_path=subtitle,
        output_path=default_output_for(subtitle),
        options=sync_options(**options),  # type: ignore[arg-type]
    )


def test_real_sync_recovers_the_offset(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(6.5)
    job = _job(reference, subtitle)
    validate(job)
    result = _run_one(job)

    assert result.status is JobStatus.SUCCEEDED
    assert result.offset_seconds is not None
    assert result.offset_seconds == pytest.approx(-6.5, abs=0.25)
    assert job.output_path.is_file()
    # The score is not in ffsubsync's return value; it is scraped from its logs.
    assert result.score is not None


def test_progress_and_logs_reach_the_parent(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(3.0)
    logs: list[tuple[str, str]] = []
    job = _job(reference, subtitle)
    SyncProcess(build_spec([job])).run(
        on_log=lambda job_id, level, message: logs.append((level, message))
    )
    assert logs
    assert any("score" in message for _, message in logs)


def test_event_channel_is_not_corrupted_by_subtitle_output(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    """A result must always arrive, never be lost in stray stdout writes."""
    reference, subtitle = make_pair(2.0)
    result = _run_one(_job(reference, subtitle))
    assert result.status is JobStatus.SUCCEEDED
    assert "without reporting a result" not in result.error


def test_missing_reference_is_rejected_before_spawning(tmp_path: Path) -> None:
    from subsyncr.core.engine import EngineError

    subtitle = _write_subtitles(tmp_path / "in.srt", [1.0, 2.0])
    job = _job(tmp_path / "absent.srt", subtitle)
    with pytest.raises(EngineError, match="Reference file is missing"):
        validate(job)


def test_existing_output_is_not_silently_replaced(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    from subsyncr.core.engine import EngineError

    reference, subtitle = make_pair(1.0)
    job = _job(reference, subtitle)
    job.output_path.write_text("existing work", encoding="utf-8")
    with pytest.raises(EngineError, match="Output already exists"):
        validate(job)
    job.options.files.replace_output = True
    validate(job)


def test_low_quality_gate_reports_skipped_not_succeeded(tmp_path: Path) -> None:
    """The trap: ffsubsync writes the ORIGINAL subtitles to the output path.

    A file appearing at the output path is therefore not evidence of success.
    """
    reference = _write_subtitles(
        tmp_path / "reference.srt", [4.0, 9.3, 15.1, 22.6, 30.0, 38.8, 44.2]
    )
    subtitle = _write_subtitles(
        tmp_path / "input.srt", [2.0, 3.1, 4.4, 5.0, 6.3, 7.7, 8.1]
    )
    job = _job(
        reference,
        subtitle,
        skip_sync_on_low_quality=True,
        # An unreachable threshold guarantees the gate trips.
        min_score=1e9,
    )
    result = _run_one(job)

    assert result.status is JobStatus.SKIPPED_LOW_QUALITY
    assert result.quality_reasons
    assert job.output_path.is_file(), "ffsubsync still writes the untouched original"


def test_cancellation_kills_the_child(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(4.0)
    job = _job(reference, subtitle)
    process = SyncProcess(build_spec([job]))
    # Cancel before it starts: the run must still terminate promptly and report.
    process.cancel()
    started = time.monotonic()
    result = process.run()[job.id]
    assert result.status is JobStatus.CANCELLED
    assert time.monotonic() - started < 30


def test_cancellation_mid_flight(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(4.0)
    job = _job(reference, subtitle)
    process = SyncProcess(build_spec([job]))
    threading.Timer(0.05, process.cancel).start()
    result = process.run()[job.id]
    # Very short jobs may finish first; either outcome proves no hang or crash.
    assert result.status in (JobStatus.CANCELLED, JobStatus.SUCCEEDED)


def test_overwrite_input_writes_in_place(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(5.0)
    before = subtitle.read_text(encoding="utf-8")
    job = _job(reference, subtitle, overwrite_input=True)
    result = _run_one(job)
    assert result.status is JobStatus.SUCCEEDED
    assert subtitle.read_text(encoding="utf-8") != before
    assert not default_output_for(subtitle).exists()


def test_overwrite_keeps_a_backup_of_the_original(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(5.0)
    original = subtitle.read_text(encoding="utf-8")
    job = _job(reference, subtitle, overwrite_input=True)
    assert _run_one(job).status is JobStatus.SUCCEEDED

    backup = subtitle.with_suffix(subtitle.suffix + ".bak")
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == original
    assert subtitle.read_text(encoding="utf-8") != original


def test_a_second_overwrite_does_not_bury_the_true_original(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    """Otherwise re-running would replace the backup with synced content."""
    reference, subtitle = make_pair(5.0)
    original = subtitle.read_text(encoding="utf-8")
    backup = subtitle.with_suffix(subtitle.suffix + ".bak")

    for _ in range(2):
        job = _job(reference, subtitle, overwrite_input=True)
        assert _run_one(job).status is JobStatus.SUCCEEDED
    assert backup.read_text(encoding="utf-8") == original


def test_backup_can_be_turned_off(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(5.0)
    job = _job(reference, subtitle, overwrite_input=True, backup_before_overwrite=False)
    assert _run_one(job).status is JobStatus.SUCCEEDED
    assert not subtitle.with_suffix(subtitle.suffix + ".bak").exists()


def test_no_backup_when_not_overwriting(
    make_pair: Callable[[float], tuple[Path, Path]],
) -> None:
    reference, subtitle = make_pair(5.0)
    assert _run_one(_job(reference, subtitle)).status is JobStatus.SUCCEEDED
    assert not subtitle.with_suffix(subtitle.suffix + ".bak").exists()
