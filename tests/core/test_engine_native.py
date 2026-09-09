"""alass and lapse: argument construction and result parsing.

The sample outputs below are verbatim from running the pinned binaries, so a
change in how either tool talks shows up here rather than as a silent loss of
the offset column.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from support import sync_options

from ffsubsyncui.core.engines import alass, lapse
from ffsubsyncui.core.engines.runner import clean_line, percent_in


def _options(**overrides: Any) -> dict[str, Any]:
    options = sync_options()
    for name, value in overrides.items():
        block, _, field = name.partition("__")
        setattr(getattr(options, block), field, value)
    return options.to_dict()


def _alass_argv(**overrides: Any) -> list[str]:
    return alass.build_arguments(
        Path("alass-cli"),
        _options(**overrides),
        "ep.mkv",
        Path("ep.srt"),
        Path("out.srt"),
    ).argv


def _lapse_argv(**overrides: Any) -> list[str]:
    return lapse.build_arguments(
        Path("lapse"),
        _options(**overrides),
        "ep.mkv",
        Path("ep.srt"),
        Path("out.srt"),
    ).argv


# -- alass ---------------------------------------------------------------


def test_alass_takes_its_three_paths_positionally() -> None:
    assert _alass_argv()[:4] == ["alass-cli", "ep.mkv", "ep.srt", "out.srt"]


def test_alass_split_penalty_is_passed_by_default() -> None:
    argv = _alass_argv()
    assert "--split-penalty" in argv
    assert argv[argv.index("--split-penalty") + 1] == "7.0"


def test_alass_no_split_is_singular() -> None:
    """The binary accepts --no-split; its README's --no-splits does not exist."""
    argv = _alass_argv(alass__split_penalty=None)
    assert "--no-split" in argv
    assert "--no-splits" not in argv
    assert "--split-penalty" not in argv


def test_alass_speed_optimization_is_disabled_with_a_value() -> None:
    argv = _alass_argv(alass__disable_speed_optimization=True)
    assert argv[argv.index("--speed-optimization") + 1] == "0"


def test_alass_is_told_where_ffmpeg_is_through_the_environment(
    tmp_path: Path,
) -> None:
    """alass ignores PATH and reads only ALASS_FFMPEG_PATH."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"):
        (binaries / name).touch()
    invocation = alass.build_arguments(
        Path("alass-cli"),
        _options(environment__ffmpeg_path=str(binaries)),
        "ep.mkv",
        Path("ep.srt"),
        Path("out.srt"),
    )
    assert Path(invocation.env["ALASS_FFMPEG_PATH"]).parent == binaries
    assert Path(invocation.env["ALASS_FFPROBE_PATH"]).parent == binaries


_ALASS_OUTPUT = """
Guessing framerate ratio...
info: 'reference file FPS/input file FPS' ratio is 1
synchronizing 'bad.srt' to reference file 'ref.srt'...
shifted block of 2 subtitles with length 0:00:10.000 by -0:00:05.001
shifted block of 1 subtitles with length 0:00:00.000 by -0:00:04.958
"""


def test_alass_offset_comes_from_the_largest_block(tmp_path: Path) -> None:
    """It reports one shift per segment; the biggest describes the file."""
    output = tmp_path / "out.srt"
    output.touch()
    outcome = alass.interpret(0, _ALASS_OUTPUT, output)
    assert outcome.status == "succeeded"
    assert outcome.offset_seconds is not None
    assert abs(outcome.offset_seconds + 5.001) < 1e-6


def test_alass_reports_no_scale_when_the_ratio_is_one(tmp_path: Path) -> None:
    """A ratio of 1 means no correction happened; saying so beats showing 1."""
    output = tmp_path / "out.srt"
    output.touch()
    assert alass.interpret(0, _ALASS_OUTPUT, output).framerate_scale_factor is None


def test_alass_reports_a_real_framerate_correction(tmp_path: Path) -> None:
    output = tmp_path / "out.srt"
    output.touch()
    text = "info: 'reference file FPS/input file FPS' ratio is 1.0427"
    assert alass.interpret(0, text, output).framerate_scale_factor == 1.0427


def test_alass_clean_exit_without_output_is_a_failure(tmp_path: Path) -> None:
    outcome = alass.interpret(0, _ALASS_OUTPUT, tmp_path / "missing.srt")
    assert outcome.status == "failed"


def test_alass_failure_quotes_its_error_line(tmp_path: Path) -> None:
    outcome = alass.interpret(1, "error: could not open file\n", tmp_path / "x.srt")
    assert outcome.status == "failed"
    assert "could not open file" in outcome.error


# -- lapse ---------------------------------------------------------------


def test_lapse_always_asks_for_json_and_owns_the_file_handling() -> None:
    argv = _lapse_argv()
    assert "--json" in argv
    # We take backups and stage the write ourselves; lapse must not also.
    assert "--no-backup" in argv
    assert "--no-sidecar" in argv
    assert argv[argv.index("--output") + 1] == "out.srt"


def test_lapse_does_not_force_by_default() -> None:
    """An unsure alignment should leave the original alone unless asked."""
    assert "--force" not in _lapse_argv()
    assert "--force" in _lapse_argv(lapse__force=True)


def test_lapse_mode_and_penalty_are_positional() -> None:
    argv = _lapse_argv(lapse__mode="split", lapse__split_penalty=9.0)
    assert argv[3:5] == ["split", "9.0"]


def test_lapse_default_mode_is_left_implicit() -> None:
    argv = _lapse_argv()
    assert "auto" not in argv


def test_lapse_names_auto_only_to_carry_a_changed_penalty() -> None:
    argv = _lapse_argv(lapse__split_penalty=11.0)
    assert argv[3:5] == ["auto", "11.0"]


_LAPSE_SOLID = (
    '{"mode":"auto/shifted","reference":"subtitle","offset_ms":-5000,"ratio":1,'
    '"confidence":12.5,"verdict":"solid","cues":60,"written":true,'
    '"output":"out.srt","splits":[]}'
)

_LAPSE_UNSURE = (
    '{"mode":"auto/shifted","offset_ms":-5000,"ratio":1,"confidence":1,'
    '"verdict":"unsure","written":false,"why":"not sure enough to touch the '
    'original","output":"out.srt"}'
)


def test_lapse_reads_its_json_rather_than_the_exit_code(tmp_path: Path) -> None:
    output = tmp_path / "out.srt"
    output.touch()
    outcome = lapse.interpret(0, _LAPSE_SOLID, output)
    assert outcome.status == "succeeded"
    assert outcome.offset_seconds == -5.0
    assert outcome.score == 12.5
    assert outcome.framerate_scale_factor is None  # ratio 1 means untouched


def test_lapse_declining_to_write_is_low_quality_not_failure(
    tmp_path: Path,
) -> None:
    """The original is untouched, exactly as ffsubsync's quality gate leaves it."""
    outcome = lapse.interpret(2, _LAPSE_UNSURE, tmp_path / "out.srt")
    assert outcome.status == "skipped_low_quality"
    assert outcome.quality_reasons == ["not sure enough to touch the original"]


def test_lapse_hard_error_without_json_is_a_failure(tmp_path: Path) -> None:
    outcome = lapse.interpret(1, "ERROR could not open the file", tmp_path / "o.srt")
    assert outcome.status == "failed"
    assert "could not open the file" in outcome.error


def test_a_forced_unsure_result_succeeds_but_keeps_its_reasons(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out.srt"
    output.touch()
    forced = _LAPSE_UNSURE.replace('"written":false', '"written":true')
    outcome = lapse.interpret(0, forced, output)
    assert outcome.status == "succeeded"
    assert outcome.quality_reasons  # the shift was applied, with reservations


# -- shared output handling ----------------------------------------------


def test_progress_percentages_are_read_from_a_progress_bar() -> None:
    line = "3 / 6 [========>-----] 50.00 % 24271.84/s 0s"
    assert percent_in(line) == 0.5
    assert percent_in("synchronizing 'bad.srt'...") is None


def test_ansi_escapes_are_stripped_from_forwarded_lines() -> None:
    assert clean_line("\x1b[2K\x1b[Ashifted block") == "shifted block"
