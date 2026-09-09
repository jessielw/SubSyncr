"""Argument construction and result interpretation for the ffsubsync engine.

These encode the ffsubsync behaviours that are easy to get wrong and expensive
to discover at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from support import sync_options

from subsyncr.core.engines.ffsubsync import build_arguments
from subsyncr.core.models import SyncOptions

_ITEM: dict[str, Any] = {
    "id": "job-1",
    "subtitle": "ep.en.srt",
    "output": "ep.en.synced.srt",
}


def _spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "engine": "ffsubsync",
        "reference": "ep.mkv",
        "options": SyncOptions().to_dict(),
        "serialize_speech": False,
        "items": [_ITEM],
    }
    spec.update(overrides)
    return spec


def _args(**overrides: Any) -> list[str]:
    spec = _spec(**overrides)
    return build_arguments(spec, spec["items"][0])


def _pairs(arguments: list[str], flag: str) -> str | None:
    return arguments[arguments.index(flag) + 1] if flag in arguments else None


def test_output_is_always_explicit() -> None:
    """Without -o, ffsubsync writes subtitle bytes to our event channel."""
    arguments = _args()
    assert "-o" in arguments
    assert _pairs(arguments, "-o") == "ep.en.synced.srt"


def test_input_is_always_explicit() -> None:
    """Omitting -i makes ffsubsync call sys.stdin.isatty(), which can be None."""
    assert "-i" in _args()


def test_gui_mode_is_never_passed() -> None:
    """--gui-mode redirects tqdm to stdout and forces its own output path."""
    assert "--gui-mode" not in _args()


def test_overwrite_input_replaces_output_flag() -> None:
    options = sync_options(overwrite_input=True).to_dict()
    arguments = _args(options=options)
    assert "--overwrite-input" in arguments
    # ffsubsync rejects --overwrite-input together with -o.
    assert "-o" not in arguments


def test_vad_is_passed_for_video_references() -> None:
    arguments = _args(options=sync_options(vad="auditok").to_dict())
    assert _pairs(arguments, "--vad") == "auditok"


def test_vad_is_omitted_for_subtitle_and_speech_references() -> None:
    """Otherwise ffsubsync warns "Vad specified, but reference was not a movie"."""
    for reference in ("reference.srt", "cached.npz"):
        arguments = _args(reference=reference)
        assert "--vad" not in arguments, reference


def test_optional_arguments_are_omitted_when_unset() -> None:
    arguments = _args()
    assert "--split-penalty" not in arguments
    assert "--max-duration-seconds" not in arguments
    assert "--multi-segment-sync" not in arguments


def test_optional_arguments_appear_when_set() -> None:
    options = sync_options(
        split_penalty=5.0,
        max_duration_seconds=600.0,
        multi_segment_sync=True,
        segment_count=12,
        gss=True,
        no_fix_framerate=True,
        reference_stream="a:1",
    ).to_dict()
    arguments = _args(options=options)
    assert _pairs(arguments, "--split-penalty") == "5.0"
    assert _pairs(arguments, "--max-duration-seconds") == "600.0"
    assert _pairs(arguments, "--segment-count") == "12"
    assert _pairs(arguments, "--reference-stream") == "a:1"
    assert "--multi-segment-sync" in arguments
    assert "--gss" in arguments
    assert "--no-fix-framerate" in arguments


def test_quality_gate_arguments_travel_together() -> None:
    options = sync_options(
        skip_sync_on_low_quality=True,
        min_score=2.5,
        quality_max_offset_seconds=20.0,
        max_framerate_deviation=0.05,
    ).to_dict()
    arguments = _args(options=options)
    assert "--skip-sync-on-low-quality" in arguments
    assert _pairs(arguments, "--min-score") == "2.5"
    assert _pairs(arguments, "--quality-max-offset-seconds") == "20.0"
    assert _pairs(arguments, "--max-framerate-deviation") == "0.05"


def test_serialize_speech_is_not_an_argv_flag() -> None:
    """The worker writes the speech array itself.

    ffsubsync's --serialize-speech only fires on its own extraction path, which
    would defeat sharing one in-memory extraction across the group.
    """
    assert "--serialize-speech" not in _args(serialize_speech=True)


def test_quality_thresholds_are_omitted_when_the_gate_is_off() -> None:
    arguments = _args()
    assert "--skip-sync-on-low-quality" not in arguments
    assert "--min-score" not in arguments


def test_arguments_parse_with_the_real_ffsubsync_parser() -> None:
    """The strongest check available: ffsubsync's own parser accepts them."""
    from ffsubsync.ffsubsync import make_parser

    options = sync_options(
        vad="auditok",
        split_penalty=5.0,
        multi_segment_sync=True,
        max_duration_seconds=300.0,
        skip_sync_on_low_quality=True,
        reference_stream="a:0",
        ffmpeg_path=str(Path.cwd()),
    ).to_dict()
    arguments = _args(options=options)
    namespace = make_parser().parse_args(arguments)
    assert namespace.reference == "ep.mkv"
    assert namespace.srtin == ["ep.en.srt"]
    assert namespace.srtout == "ep.en.synced.srt"
    assert namespace.vad == "auditok"
    assert namespace.skip_sync_on_low_quality is True


def test_fast_mode_passes_segment_sampling() -> None:
    """Fast mode is multi-segment sampling, nothing more."""
    options = sync_options(multi_segment_sync=True, segment_count=6).to_dict()
    arguments = _args(options=options)
    assert "--multi-segment-sync" in arguments
    assert _pairs(arguments, "--segment-count") == "6"
    # --max-duration-seconds is deliberately NOT part of fast mode: truncating
    # the reference can return a confidently wrong offset.
    assert "--max-duration-seconds" not in arguments
