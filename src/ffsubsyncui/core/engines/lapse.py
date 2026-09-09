"""The lapse engine.

lapse is the only tool of the four that reports a structured result, so this
module asks for ``--json`` and reads that rather than scraping a log. What comes
back carries the offset, the framerate ratio, a confidence, and an explicit
verdict on whether the alignment was trustworthy enough to write.

That verdict is the interesting part: left to itself lapse refuses to write an
alignment it is unsure about, which is exactly the ``skipped_low_quality``
outcome the queue already understands - the original is untouched and the job is
neither a success nor a failure. Its exit codes are less expressive than its
JSON, so the JSON wins wherever the two could disagree.

Choosing ``--json`` costs the progress bar, since lapse emits one or the other.
An exact offset is worth more than a moving bar for a tool this fast.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from ffsubsyncui.core.engines.base import EngineId
from ffsubsyncui.core.engines.runner import Invocation, Outcome, run_pairwise
from ffsubsyncui.core.worker import CancelToken, EventChannel

_JSON_LINE = re.compile(r"^\s*\{.*\}\s*$")

# Verdicts that mean "I found an alignment but would not stake anything on it".
_UNSURE = frozenset({"unsure", "weak", "shaky"})

_MODES = ("auto", "split", "nosplit", "ols")


def build_arguments(
    executable: Path,
    options: dict[str, Any],
    reference: str,
    subtitle: Path,
    output: Path,
) -> Invocation:
    """lapse takes paths, then mode and penalty positionally, then flags."""
    tuning: dict[str, Any] = options.get("lapse") or {}
    argv = [str(executable), reference, str(subtitle)]

    mode = str(tuning.get("mode", "auto"))
    penalty = tuning.get("split_penalty", 6.0)
    if mode not in _MODES:
        mode = "auto"
    if mode in ("split", "ols", "nosplit"):
        argv.append(mode)
        if mode == "split":
            argv.append(str(penalty))
    elif penalty != 6.0:
        # "auto" is the default and only needs naming when the penalty differs.
        argv.extend(("auto", str(penalty)))

    argv.extend(("--output", str(output)))
    # We do our own backups and write through a temp file, so lapse must not
    # also rename things behind us.
    argv.extend(("--no-backup", "--no-sidecar"))
    argv.append("--json")
    if (confidence := tuning.get("confidence")) is not None:
        argv.extend(("--confidence", str(confidence)))
    if tuning.get("force"):
        # Apply the shift even where lapse would rather not. The result is
        # still reported with its reasons attached.
        argv.append("--force")

    if not tuning.get("use_embedded", True):
        argv.append("--no-embedded")
    if tuning.get("full_scan"):
        argv.append("--full-scan")
    if tuning.get("no_cache"):
        argv.append("--no-cache")

    return Invocation(argv=argv)


def _payload(output: str) -> dict[str, Any] | None:
    """The last JSON object lapse printed, if it printed one at all."""
    for line in reversed(output.splitlines()):
        if not _JSON_LINE.match(line):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def interpret(returncode: int, output: str, output_path: Path) -> Outcome:
    report = _payload(output)
    if report is None:
        # A hard failure - a missing file, say - is plain text on stderr.
        return Outcome(status="failed", error=_failure_reason(output, returncode))

    offset_ms = report.get("offset_ms")
    ratio = report.get("ratio")
    outcome = Outcome(
        status="succeeded",
        offset_seconds=float(offset_ms) / 1000.0
        if isinstance(offset_ms, int | float)
        else None,
        # A ratio of 1 means no framerate correction was needed.
        framerate_scale_factor=float(ratio)
        if isinstance(ratio, int | float) and float(ratio) != 1.0
        else None,
        score=float(report["confidence"])
        if isinstance(report.get("confidence"), int | float)
        else None,
    )

    verdict = str(report.get("verdict", "")).lower()
    why = str(report.get("why", "")).strip()
    unsure = verdict in _UNSURE

    if not report.get("written", False) or not output_path.is_file():
        if unsure:
            # lapse declined on purpose and the original is untouched. That is
            # the same bargain as ffsubsync's quality gate, so it gets the same
            # status: not a success, but emphatically not a failure either.
            outcome.status = "skipped_low_quality"
            outcome.quality_reasons = [why or f"lapse verdict: {verdict}"]
            return outcome
        outcome.status = "failed"
        outcome.error = why or f"lapse wrote no output (exit {returncode})"
        return outcome

    if unsure:
        # Written anyway, because "force" is on. The shift was applied, so this
        # is a success - but one the user should see the reservations about.
        outcome.quality_reasons = [why or f"lapse verdict: {verdict}"]
    return outcome


def _failure_reason(output: str, returncode: int) -> str:
    for line in reversed(output.splitlines()):
        if line.strip():
            return line.strip()
    return f"lapse exited with code {returncode}"


def run_group(spec: dict[str, Any], channel: EventChannel, cancel: CancelToken) -> int:
    return run_pairwise(
        spec,
        channel,
        cancel,
        engine=EngineId.LAPSE,
        invoke=build_arguments,
        interpret=interpret,
    )
