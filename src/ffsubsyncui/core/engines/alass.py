"""The alass engine.

alass aligns by voice activity and is unusually good at subtitles that drift
because of advert breaks or a different cut, since it can split the subtitle
into segments and shift each one independently.

Flag names here come from ``alass-cli --help`` at the pinned version, not from
its README, which documents a ``--no-splits`` that the binary does not accept.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ffsubsyncui.core import binaries, encoding as encodings
from ffsubsyncui.core.engines.base import EngineId
from ffsubsyncui.core.engines.runner import (
    Invocation,
    Outcome,
    run_pairwise,
)
from ffsubsyncui.core.worker import CancelToken, EventChannel

# Reference formats alass parses as text, and so needs an encoding for.
_TEXT_REFERENCES = frozenset({".srt", ".ass", ".ssa", ".sub"})

# "info: 'reference file FPS/input file FPS' ratio is 1"
_RATIO = re.compile(r"ratio is (-?\d+(?:\.\d+)?)")

# "shifted block of 2 subtitles with length 0:00:10.000 by -0:00:05.001"
_SHIFT = re.compile(
    r"shifted block of (\d+) subtitles? with length \S+ "
    r"by (-?)(\d+):(\d+):(\d+(?:\.\d+)?)"
)


def build_arguments(
    executable: Path,
    options: dict[str, Any],
    reference: str,
    subtitle: Path,
    output: Path,
) -> Invocation:
    """alass takes its three paths positionally, then flags."""
    tuning: dict[str, Any] = options.get("alass") or {}
    environment: dict[str, Any] = options.get("environment") or {}
    files: dict[str, Any] = options.get("files") or {}
    configured = str(files.get("encoding", "infer"))
    argv = [str(executable), reference, str(subtitle), str(output)]

    # alass has no charset detection of its own: given a cp1251 subtitle and no
    # --encoding-inc it reads mojibake and aligns it happily.
    argv.extend(("--encoding-inc", encodings.resolve(subtitle, configured)))
    reference_path = Path(reference)
    if reference_path.suffix.lower() in _TEXT_REFERENCES and reference_path.is_file():
        argv.extend(("--encoding-ref", encodings.resolve(reference_path, configured)))

    penalty = tuning.get("split_penalty", 7.0)
    if penalty is None:
        # Shift everything by one offset and never cut - much faster, and the
        # right choice when the subtitle is simply late rather than drifting.
        argv.append("--no-split")
    else:
        argv.extend(("--split-penalty", str(penalty)))
    if tuning.get("disable_fps_guessing"):
        argv.append("--disable-fps-guessing")
    if tuning.get("disable_speed_optimization"):
        argv.extend(("--speed-optimization", "0"))
    if (interval := tuning.get("interval_ms")) is not None:
        argv.extend(("--interval", str(int(interval))))
    if tuning.get("allow_negative_timestamps"):
        argv.append("--allow-negative-timestamps")

    return Invocation(
        argv=argv,
        # alass shells out to ffmpeg and will only look where these point.
        env=binaries.alass_environment(
            executable, str(environment.get("ffmpeg_path", ""))
        ),
    )


def interpret(returncode: int, output: str, output_path: Path) -> Outcome:
    """Read alass's log for the numbers it does not return any other way."""
    if returncode != 0:
        return Outcome(
            status="failed",
            error=_failure_reason(output, returncode),
        )
    if not output_path.is_file():
        return Outcome(
            status="failed", error="alass exited cleanly but wrote no output"
        )

    scale: float | None = None
    if ratio := _RATIO.search(output):
        with_value = float(ratio.group(1))
        # A ratio of exactly 1 means it found no framerate difference; saying
        # so is more useful than reporting a correction that did not happen.
        scale = with_value if with_value != 1.0 else None

    # alass reports one shift per segment. The segment covering the most
    # subtitles is the one that describes the file.
    best_count = -1
    offset: float | None = None
    for match in _SHIFT.finditer(output):
        count = int(match.group(1))
        if count <= best_count:
            continue
        best_count = count
        sign = -1.0 if match.group(2) else 1.0
        seconds = (
            int(match.group(3)) * 3600
            + int(match.group(4)) * 60
            + float(match.group(5))
        )
        offset = sign * seconds

    return Outcome(
        status="succeeded", offset_seconds=offset, framerate_scale_factor=scale
    )


def _failure_reason(output: str, returncode: int) -> str:
    for line in reversed(output.splitlines()):
        if line.lower().startswith(("error", "failed")):
            return line
    tail = output.splitlines()[-1:] or [""]
    return tail[0] or f"alass exited with code {returncode}"


def run_group(spec: dict[str, Any], channel: EventChannel, cancel: CancelToken) -> int:
    return run_pairwise(
        spec,
        channel,
        cancel,
        engine=EngineId.ALASS,
        invoke=build_arguments,
        interpret=interpret,
    )
