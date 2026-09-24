"""Job and option models, including their persistence round trip."""

from __future__ import annotations

from pathlib import Path

import pytest
from support import sync_options

from subsyncr.core.engines import EngineId
from subsyncr.core.models import (
    FileOptions,
    JobStatus,
    LapseOptions,
    OutputPolicy,
    SyncJob,
    SyncOptions,
    default_output_for,
    resolve_output,
)


def test_options_round_trip() -> None:
    options = sync_options(
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
    assert restored.ffsubsync.vad == "webrtc"
    default = SyncOptions().ffsubsync
    assert restored.ffsubsync.max_offset_seconds == default.max_offset_seconds
    assert restored.ffsubsync.segment_count == default.segment_count


def test_options_from_garbage_yields_defaults() -> None:
    assert SyncOptions.from_dict("nonsense") == SyncOptions()


def test_none_is_preserved_for_optional_options() -> None:
    """split_penalty and max_duration mean something different when unset."""
    options = SyncOptions.from_dict({"vad": "webrtc", "split_penalty": None})
    assert options.ffsubsync.split_penalty is None


def test_speech_cache_key_tracks_extraction_settings() -> None:
    def key(**overrides: object) -> object:
        return sync_options(**overrides).ffsubsync.speech_cache_key()

    base = key()
    assert base == key()
    # Anything baked into the serialized array must change the key...
    assert base != key(vad="auditok")
    assert base != key(start_seconds=30)
    # ...and anything that is not must leave it alone.
    assert base == key(encoding="utf-8")


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
    job.options.files.overwrite_input = True
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


# -- multi-engine options ------------------------------------------------


def test_a_legacy_flat_config_is_lifted_into_engine_blocks() -> None:
    """Configs written before multi-engine were one flat block of ffsubsync."""
    restored = SyncOptions.from_dict(
        {
            "vad": "auditok",
            "gss": True,
            "split_penalty": 7.5,
            "overwrite_input": True,
            "backup_before_overwrite": False,
            "encoding": "cp1251",
            "ffmpeg_path": "C:/ffmpeg/bin",
        }
    )
    assert restored.engine is EngineId.FFSUBSYNC
    assert restored.ffsubsync.vad == "auditok"
    assert restored.ffsubsync.gss is True
    assert restored.ffsubsync.split_penalty == 7.5
    assert restored.files.overwrite_input is True
    assert restored.files.backup_before_overwrite is False
    assert restored.files.encoding == "cp1251"
    assert restored.environment.ffmpeg_path == "C:/ffmpeg/bin"


def test_a_nested_config_is_not_mistaken_for_a_legacy_one() -> None:
    options = sync_options(vad="webrtc")
    options.engine = EngineId.FFSUBSYNC
    assert SyncOptions.from_dict(options.to_dict()) == options


def test_every_engine_keeps_its_settings_when_another_is_selected() -> None:
    """Switching engines must not discard configuration the user has set."""
    options = SyncOptions()
    options.alass.split_penalty = 12.0
    options.lapse.mode = "ols"
    options.engine = EngineId.ALASS
    restored = SyncOptions.from_dict(options.to_dict())
    assert restored.alass.split_penalty == 12.0
    assert restored.lapse.mode == "ols"
    assert restored.engine is EngineId.ALASS


def test_copy_is_deep_enough_to_isolate_jobs() -> None:
    """A shallow copy would let one job's overwrite flag set every job's."""
    original = SyncOptions()
    clone = original.copy()
    clone.files.overwrite_input = True
    clone.ffsubsync.vad = "auditok"
    assert original.files.overwrite_input is False
    assert original.ffsubsync.vad != "auditok"


def test_with_engine_runs_it_first_and_keeps_the_rest_of_the_chain() -> None:
    original = SyncOptions(
        engine=EngineId.FFSUBSYNC,
        fallback_chain=(EngineId.ALASS, EngineId.LAPSE),
    )
    picked = original.with_engine(EngineId.ALASS)
    assert picked.engine is EngineId.ALASS
    # Never retried with the engine that just failed.
    assert picked.fallback_chain == (EngineId.LAPSE,)
    picked.files.overwrite_input = True
    assert original.engine is EngineId.FFSUBSYNC
    assert original.fallback_chain == (EngineId.ALASS, EngineId.LAPSE)
    assert original.files.overwrite_input is False


def test_identity_ignores_engines_that_are_not_running() -> None:
    """Editing alass settings must not split an ffsubsync group in two."""
    base = SyncOptions()
    other = SyncOptions()
    other.alass.disable_fps_guessing = True
    assert base.identity() == other.identity()
    other.ffsubsync.vad = "auditok"
    assert base.identity() != other.identity()


def test_an_unknown_engine_falls_back_rather_than_failing() -> None:
    assert SyncOptions.from_dict({"engine": "wishful"}).engine is EngineId.FFSUBSYNC


def test_fallback_chain_drops_unknowns_and_duplicates() -> None:
    restored = SyncOptions.from_dict(
        {"engine": "ffsubsync", "fallback_chain": ["alass", "nope", "alass", "lapse"]}
    )
    assert restored.fallback_chain == (EngineId.ALASS, EngineId.LAPSE)


def test_lapse_mode_falls_back_when_hand_edited() -> None:
    assert LapseOptions.from_dict({"mode": "sideways"}).mode == "auto"
    assert LapseOptions.from_dict({"mode": "ols"}).mode == "ols"


# -- output policy -------------------------------------------------------


def _files(policy: OutputPolicy, folder: str = "") -> FileOptions:
    return FileOptions(output_policy=policy, output_folder=folder)


def test_the_default_policy_writes_beside_the_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "media" / "Show.S01E01.mkv"
    subtitle = tmp_path / "subs" / "Show.S01E01.en.srt"
    resolved = resolve_output(video, subtitle, FileOptions())
    assert resolved == subtitle.parent / "Show.S01E01.en.synced.srt"


def test_overwrite_returns_the_subtitle_itself(tmp_path: Path) -> None:
    subtitle = tmp_path / "ep.en.srt"
    files = _files(OutputPolicy.OVERWRITE)
    assert resolve_output(tmp_path / "ep.mkv", subtitle, files) == subtitle


def test_beside_video_keeps_the_subtitle_name(tmp_path: Path) -> None:
    video = tmp_path / "media" / "ep.mkv"
    subtitle = tmp_path / "subs" / "ep.en.srt"
    resolved = resolve_output(video, subtitle, _files(OutputPolicy.BESIDE_VIDEO))
    assert resolved.parent == video.parent
    assert resolved.name == "ep.en.synced.srt"


def test_video_name_drops_the_suffix_so_players_auto_load_it(tmp_path: Path) -> None:
    video = tmp_path / "media" / "Show.S01E01.mkv"
    subtitle = tmp_path / "subs" / "loose-name.en.srt"
    resolved = resolve_output(video, subtitle, _files(OutputPolicy.VIDEO_NAME))
    assert resolved == video.parent / "Show.S01E01.srt"


def test_a_chosen_folder_is_used(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    resolved = resolve_output(
        tmp_path / "ep.mkv",
        tmp_path / "ep.en.srt",
        _files(OutputPolicy.FOLDER, str(destination)),
    )
    assert resolved.parent == destination


def test_a_folder_that_has_gone_away_falls_back_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """Writing somewhere surprising is worse than writing beside the input."""
    subtitle = tmp_path / "ep.en.srt"
    resolved = resolve_output(
        tmp_path / "ep.mkv",
        subtitle,
        _files(OutputPolicy.FOLDER, str(tmp_path / "deleted")),
    )
    assert resolved.parent == subtitle.parent


def test_an_unwritable_extension_is_redirected_to_subrip(tmp_path: Path) -> None:
    resolved = resolve_output(
        tmp_path / "ep.mkv", tmp_path / "ep.sub", _files(OutputPolicy.BESIDE_VIDEO)
    )
    assert resolved.suffix == ".srt"


def test_overwrite_input_is_a_view_of_the_policy() -> None:
    files = FileOptions()
    assert files.overwrite_input is False
    files.overwrite_input = True
    assert files.output_policy is OutputPolicy.OVERWRITE
    files.overwrite_input = False
    assert files.output_policy is OutputPolicy.BESIDE_SUBTITLE


def test_clearing_overwrite_leaves_another_policy_alone() -> None:
    """Turning off "write in place" must not silently discard a chosen folder."""
    files = FileOptions(output_policy=OutputPolicy.BESIDE_VIDEO)
    files.overwrite_input = False
    assert files.output_policy is OutputPolicy.BESIDE_VIDEO


def test_a_legacy_overwrite_flag_becomes_the_overwrite_policy() -> None:
    restored = FileOptions.from_dict({"overwrite_input": True})
    assert restored.output_policy is OutputPolicy.OVERWRITE
    assert restored.overwrite_input is True


def test_a_hand_edited_policy_falls_back_to_the_safe_one() -> None:
    assert (
        FileOptions.from_dict({"output_policy": "somewhere else"}).output_policy
        is OutputPolicy.BESIDE_SUBTITLE
    )
