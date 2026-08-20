"""Argument construction and result interpretation in the sync child.

These encode the ffsubsync behaviours that are easy to get wrong and expensive
to discover at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ffsubsyncui.core.models import SyncOptions
from ffsubsyncui.core.worker import build_arguments


def _spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "reference": "ep.mkv",
        "subtitle": "ep.en.srt",
        "output": "ep.en.synced.srt",
        "options": SyncOptions().to_dict(),
        "serialize_speech": False,
    }
    spec.update(overrides)
    return spec


def _pairs(arguments: list[str], flag: str) -> str | None:
    return arguments[arguments.index(flag) + 1] if flag in arguments else None


def test_output_is_always_explicit() -> None:
    """Without -o, ffsubsync writes subtitle bytes to our event channel."""
    arguments = build_arguments(_spec())
    assert "-o" in arguments
    assert _pairs(arguments, "-o") == "ep.en.synced.srt"


def test_input_is_always_explicit() -> None:
    """Omitting -i makes ffsubsync call sys.stdin.isatty(), which can be None."""
    assert "-i" in build_arguments(_spec())


def test_gui_mode_is_never_passed() -> None:
    """--gui-mode redirects tqdm to stdout and forces its own output path."""
    assert "--gui-mode" not in build_arguments(_spec())


def test_overwrite_input_replaces_output_flag() -> None:
    options = SyncOptions(overwrite_input=True).to_dict()
    arguments = build_arguments(_spec(options=options))
    assert "--overwrite-input" in arguments
    # ffsubsync rejects --overwrite-input together with -o.
    assert "-o" not in arguments


def test_vad_is_passed_for_video_references() -> None:
    arguments = build_arguments(_spec(options=SyncOptions(vad="auditok").to_dict()))
    assert _pairs(arguments, "--vad") == "auditok"


def test_vad_is_omitted_for_subtitle_and_speech_references() -> None:
    """Otherwise ffsubsync warns "Vad specified, but reference was not a movie"."""
    for reference in ("reference.srt", "cached.npz"):
        arguments = build_arguments(_spec(reference=reference))
        assert "--vad" not in arguments, reference


def test_optional_arguments_are_omitted_when_unset() -> None:
    arguments = build_arguments(_spec())
    assert "--split-penalty" not in arguments
    assert "--max-duration-seconds" not in arguments
    assert "--multi-segment-sync" not in arguments
    assert "--serialize-speech" not in arguments


def test_optional_arguments_appear_when_set() -> None:
    options = SyncOptions(
        split_penalty=5.0,
        max_duration_seconds=600.0,
        multi_segment_sync=True,
        segment_count=12,
        gss=True,
        no_fix_framerate=True,
        reference_stream="a:1",
    ).to_dict()
    arguments = build_arguments(_spec(options=options, serialize_speech=True))
    assert _pairs(arguments, "--split-penalty") == "5.0"
    assert _pairs(arguments, "--max-duration-seconds") == "600.0"
    assert _pairs(arguments, "--segment-count") == "12"
    assert _pairs(arguments, "--reference-stream") == "a:1"
    assert "--multi-segment-sync" in arguments
    assert "--gss" in arguments
    assert "--no-fix-framerate" in arguments
    assert "--serialize-speech" in arguments


def test_quality_gate_arguments_travel_together() -> None:
    options = SyncOptions(
        skip_sync_on_low_quality=True,
        min_score=2.5,
        quality_max_offset_seconds=20.0,
        max_framerate_deviation=0.05,
    ).to_dict()
    arguments = build_arguments(_spec(options=options))
    assert "--skip-sync-on-low-quality" in arguments
    assert _pairs(arguments, "--min-score") == "2.5"
    assert _pairs(arguments, "--quality-max-offset-seconds") == "20.0"
    assert _pairs(arguments, "--max-framerate-deviation") == "0.05"


def test_quality_thresholds_are_omitted_when_the_gate_is_off() -> None:
    arguments = build_arguments(_spec())
    assert "--skip-sync-on-low-quality" not in arguments
    assert "--min-score" not in arguments


def test_arguments_parse_with_the_real_ffsubsync_parser() -> None:
    """The strongest check available: ffsubsync's own parser accepts them."""
    from ffsubsync.ffsubsync import make_parser

    options = SyncOptions(
        vad="auditok",
        split_penalty=5.0,
        multi_segment_sync=True,
        max_duration_seconds=300.0,
        skip_sync_on_low_quality=True,
        reference_stream="a:0",
        ffmpeg_path=str(Path.cwd()),
    ).to_dict()
    arguments = build_arguments(_spec(options=options, serialize_speech=True))
    namespace = make_parser().parse_args(arguments)
    assert namespace.reference == "ep.mkv"
    assert namespace.srtin == ["ep.en.srt"]
    assert namespace.srtout == "ep.en.synced.srt"
    assert namespace.vad == "auditok"
    assert namespace.skip_sync_on_low_quality is True
