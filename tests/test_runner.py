from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ffsubsyncui.domain import SyncJob, SyncOptions
from ffsubsyncui.runner import FFSubsyncRunner, RunnerError


def _files(tmp_path: Path) -> SyncJob:
    reference = tmp_path / "reference.srt"
    subtitle = tmp_path / "input.srt"
    reference.write_text("", encoding="utf-8")
    subtitle.write_text("", encoding="utf-8")
    return SyncJob(reference, subtitle, tmp_path / "output.srt")


def test_build_arguments_maps_typed_options(tmp_path: Path) -> None:
    job = _files(tmp_path)
    job.options = SyncOptions(
        quality_gate=True,
        no_fix_framerate=True,
        reference_stream="s:1",
        ffmpeg_path="/tools/ffmpeg",
    )
    arguments = FFSubsyncRunner().build_arguments(job)
    assert "--skip-sync-on-low-quality" in arguments
    assert "--no-fix-framerate" in arguments
    assert arguments[arguments.index("--reference-stream") + 1] == "s:1"


def test_existing_output_requires_explicit_permission(tmp_path: Path) -> None:
    job = _files(tmp_path)
    job.output_path.write_text("existing", encoding="utf-8")
    with pytest.raises(RunnerError, match="Output already exists"):
        FFSubsyncRunner().validate(job)


def test_runner_converts_upstream_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = _files(tmp_path)

    def fake_run(namespace: object, callback: object) -> dict[str, object]:
        del namespace
        callback(SimpleNamespace(fraction=0.5))  # type: ignore[operator]
        job.output_path.write_text("synced", encoding="utf-8")
        return {
            "retval": 0,
            "sync_was_successful": True,
            "offset_seconds": 1.25,
            "framerate_scale_factor": 1.0,
        }

    monkeypatch.setattr("ffsubsyncui.runner.ffsubsync.run", fake_run)
    progress: list[tuple[float | None, str]] = []
    result = FFSubsyncRunner().run(
        job,
        progress_callback=lambda value, stage: progress.append((value, stage)),
        log_callback=lambda _: None,
    )
    assert result.success
    assert result.offset_seconds == 1.25
    assert (0.5, "Extracting audio") in progress
