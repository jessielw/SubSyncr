"""Child process that syncs one reference against one or more subtitles.

Run as ``python -m ffsubsyncui.core.worker``, or as ``<frozen exe> --sync-worker``
when packaged. Reads a JSON job-group spec on stdin and writes newline-delimited
JSON events on stdout.

Isolating ffsubsync in its own process buys three things that are impossible
in-process:

* **Cancellation.** ffsubsync exposes no cancel hook, but a process can be killed.
* **Memory.** Everything a sync allocated is returned to the OS when it exits.
* **Root logger safety.** ``import ffsubsync`` calls ``logging.basicConfig()``,
  which would otherwise reconfigure the host application's logging.

A *group* is one reference plus every subtitle that syncs against it, usually
the language variants of one episode. Extracting speech from the reference is by
far the most expensive step, so the whole group shares a single extraction held
in memory - about fifteen times faster per sibling than paying process startup
and a serialized speech array for each one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, TextIO

from ffsubsyncui.core.scanner import needs_ffmpeg

# Log lines that carry results ffsubsync never returns to its caller.
_SCORE = re.compile(r"^score:\s*(-?\d+(?:\.\d+)?)")
_OFFSET = re.compile(r"^offset seconds:\s*(-?\d+(?:\.\d+)?)")
_SCALE = re.compile(r"^framerate scale factor:\s*(-?\d+(?:\.\d+)?)")
_LOW_QUALITY = re.compile(r"^low-quality alignment \((.*)\); leaving subtitles")


class EventChannel:
    """Newline-delimited JSON writer over a private copy of stdout."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: str, **payload: Any) -> None:
        payload["event"] = event
        try:
            self._stream.write(json.dumps(payload, default=str) + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            # The parent went away. Nothing useful left to do.
            pass


def claim_stdout() -> TextIO:
    """Take exclusive ownership of fd 1 for the event channel.

    Anything else that writes to stdout - a stray print, ffsubsync writing
    subtitles to ``sys.stdout.fileno()`` when ``-o`` is missing - is rerouted to
    stderr so it can never corrupt the protocol.

    The parent always supplies real pipes, but a windowed frozen build leaves
    ``sys.stdout``/``sys.stderr`` as ``None``, which breaks any library that
    writes to them (ffsubsync installs a Rich handler on stderr at import).
    Both are rebound to the descriptors we know are valid.
    """
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        diagnostics: TextIO = os.fdopen(
            2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False
        )
    except OSError:
        diagnostics = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = diagnostics
    sys.stderr = diagnostics
    return os.fdopen(saved_fd, "w", encoding="utf-8", errors="replace", buffering=1)


class _ResultScraper(logging.Handler):
    """Forward ffsubsync log records and mine them for hidden results.

    ``item_id`` tags every record with whichever subtitle is being synced, so a
    group's shared extraction and its per-subtitle work stay distinguishable.
    """

    def __init__(self, channel: EventChannel) -> None:
        super().__init__(logging.INFO)
        self._channel = channel
        self.item_id: str | None = None
        self.reset()

    def reset(self) -> None:
        self.score: float | None = None
        self.offset_seconds: float | None = None
        self.framerate_scale_factor: float | None = None
        self.low_quality_reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        if match := _SCORE.match(message):
            self.score = float(match[1])
        elif match := _OFFSET.match(message):
            self.offset_seconds = float(match[1])
        elif match := _SCALE.match(message):
            self.framerate_scale_factor = float(match[1])
        elif match := _LOW_QUALITY.match(message):
            self.low_quality_reasons = [
                reason.strip() for reason in match[1].split(";") if reason.strip()
            ]
        self._channel.emit(
            "log", job_id=self.item_id, level=record.levelname, message=message
        )


def build_arguments(spec: dict[str, Any], item: dict[str, Any]) -> list[str]:
    """Translate a group spec plus one of its subtitles into an ffsubsync argv.

    Only the first item builds the namespace; the rest are swapped in afterwards
    by rebinding ``srtin``/``srtout``.
    """
    options: dict[str, Any] = spec.get("options") or {}
    reference = str(spec["reference"])
    arguments = [reference, "-i", str(item["subtitle"])]

    if options.get("overwrite_input"):
        arguments.append("--overwrite-input")
    else:
        # Always explicit. With no -o, ffsubsync writes subtitle bytes straight
        # to stdout, and validate_args calls sys.stdin.isatty() when -i is absent.
        arguments.extend(("-o", str(item["output"])))

    arguments.extend(("--encoding", str(options.get("encoding", "infer"))))
    arguments.extend(
        ("--output-encoding", str(options.get("output_encoding", "utf-8")))
    )
    arguments.extend(
        ("--max-offset-seconds", str(options.get("max_offset_seconds", 60.0)))
    )
    arguments.extend(("--start-seconds", str(int(options.get("start_seconds", 0)))))
    arguments.extend(("--frame-rate", str(int(options.get("frame_rate", 48000)))))
    arguments.extend(("--non-speech-label", str(options.get("non_speech_label", 0.0))))
    arguments.extend(
        ("--apply-offset-seconds", str(options.get("apply_offset_seconds", 0.0)))
    )

    # VAD only applies when speech has to be detected in audio. Subtitle and
    # serialized-speech references warn ("Vad specified, but reference was not
    # a movie") and ignore it.
    if needs_ffmpeg(Path(reference)) and options.get("vad"):
        arguments.extend(("--vad", str(options["vad"])))

    if options.get("no_fix_framerate"):
        arguments.append("--no-fix-framerate")
    if options.get("gss"):
        arguments.append("--gss")
    if stream := str(options.get("reference_stream", "")).strip():
        arguments.extend(("--reference-stream", stream))
    if ffmpeg_path := str(options.get("ffmpeg_path", "")).strip():
        arguments.extend(("--ffmpeg-path", ffmpeg_path))

    if (split_penalty := options.get("split_penalty")) is not None:
        arguments.extend(("--split-penalty", str(split_penalty)))
    if options.get("multi_segment_sync"):
        arguments.append("--multi-segment-sync")
        arguments.extend(("--segment-count", str(int(options.get("segment_count", 8)))))
    if (max_duration := options.get("max_duration_seconds")) is not None:
        arguments.extend(("--max-duration-seconds", str(max_duration)))

    if options.get("skip_sync_on_low_quality"):
        arguments.append("--skip-sync-on-low-quality")
        arguments.extend(("--min-score", str(options.get("min_score", 0.0))))
        arguments.extend(
            (
                "--quality-max-offset-seconds",
                str(options.get("quality_max_offset_seconds", 30.0)),
            )
        )
        arguments.extend(
            (
                "--max-framerate-deviation",
                str(options.get("max_framerate_deviation", 0.1)),
            )
        )

    return arguments


def backup_original(subtitle: Path) -> Path | None:
    """Copy a subtitle aside before it is overwritten in place.

    An existing backup is never replaced: a second run would otherwise bury the
    true original under the already-synced version.
    """
    target = subtitle.with_suffix(subtitle.suffix + ".bak")
    if target.exists():
        return target
    try:
        shutil.copy2(subtitle, target)
    except OSError:
        return None
    return target


def _classify(synced: bool, output: Path, scraper: _ResultScraper) -> tuple[str, str]:
    """Turn ffsubsync's ambiguous signals into one status and a message."""
    if scraper.low_quality_reasons:
        # ffsubsync wrote the *original* subtitles to the output path, so the
        # file existing proves nothing here.
        return "skipped_low_quality", "; ".join(scraper.low_quality_reasons)
    if synced and output.is_file():
        return "succeeded", ""
    if synced:
        return "failed", f"ffsubsync reported success but wrote no output to {output}"
    return "failed", "ffsubsync could not align these files"


def run_group(spec: dict[str, Any], channel: EventChannel) -> int:
    """Sync every subtitle in the group against one shared extraction."""
    # Imported here so claim_stdout() runs first and the basicConfig side effect
    # of this import lands in a process we own.
    from ffsubsync.ffsubsync import (
        make_parser,
        make_reference_pipe,
        try_sync,
        validate_and_transform_args,
    )
    import numpy as np

    items: list[dict[str, Any]] = list(spec.get("items") or [])
    if not items:
        return 0
    options: dict[str, Any] = spec.get("options") or {}
    overwrite = bool(options.get("overwrite_input"))

    scraper = _ResultScraper(channel)
    logger = logging.getLogger("ffsubsync")
    logger.addHandler(scraper)

    def fail_all(message: str) -> None:
        for item in items:
            channel.emit(
                "result",
                job_id=item["id"],
                status="failed",
                return_code=1,
                error=message,
                duration_seconds=0.0,
            )

    try:
        arguments = build_arguments(spec, items[0])
        channel.emit("started", pid=os.getpid(), argv=arguments, items=len(items))

        args = validate_and_transform_args(make_parser().parse_args(arguments))
        if args is None:
            fail_all("ffsubsync rejected these arguments")
            return 1

        last_fraction = -1.0

        def on_progress(info: Any) -> None:
            nonlocal last_fraction
            raw = getattr(info, "fraction", None)
            fraction = float(raw) if raw is not None else None
            if fraction is not None and abs(fraction - last_fraction) < 0.005:
                return  # Don't flood the pipe with sub-percent updates.
            if fraction is not None:
                last_fraction = fraction
            channel.emit(
                "progress", job_id=None, fraction=fraction, stage="Extracting speech"
            )

        # The expensive step, paid once for the whole group.
        extraction_started = time.monotonic()
        reference_pipe = make_reference_pipe(args, progress_handler=on_progress)
        reference_pipe.fit(args.reference)
        extraction_seconds = time.monotonic() - extraction_started
        channel.emit("extracted", seconds=extraction_seconds)

        if spec.get("serialize_speech"):
            # ffsubsync only ever writes this beside the reference; the parent
            # moves it into the cache directory afterwards.
            try:
                np.savez_compressed(
                    os.path.splitext(args.reference)[0] + ".npz",
                    speech=reference_pipe.transform(args.reference),
                )
            except Exception as exc:
                channel.emit(
                    "log",
                    job_id=None,
                    level="WARNING",
                    message=f"could not cache speech: {exc}",
                )

        for index, item in enumerate(items):
            started = time.monotonic()
            scraper.reset()
            scraper.item_id = str(item["id"])
            subtitle = Path(str(item["subtitle"]))
            output = subtitle if overwrite else Path(str(item["output"]))
            channel.emit(
                "progress", job_id=str(item["id"]), fraction=None, stage="Aligning"
            )
            try:
                if overwrite and options.get("backup_before_overwrite", True):
                    if (saved := backup_original(subtitle)) is not None:
                        channel.emit(
                            "log",
                            job_id=str(item["id"]),
                            level="INFO",
                            message=f"kept the original at {saved.name}",
                        )
                    else:
                        channel.emit(
                            "log",
                            job_id=str(item["id"]),
                            level="WARNING",
                            message="could not write a backup; overwriting anyway",
                        )
                args.srtin = [str(subtitle)]
                args.srtout = None if overwrite else str(output)
                result: dict[str, Any] = {}
                synced = bool(try_sync(args, reference_pipe, result))
                status, error = _classify(synced, output, scraper)
                channel.emit(
                    "result",
                    job_id=str(item["id"]),
                    status=status,
                    return_code=0,
                    offset_seconds=(
                        _as_float(result.get("offset_seconds"))
                        if result.get("offset_seconds") is not None
                        else scraper.offset_seconds
                    ),
                    framerate_scale_factor=(
                        _as_float(result.get("framerate_scale_factor"))
                        if result.get("framerate_scale_factor") is not None
                        else scraper.framerate_scale_factor
                    ),
                    score=scraper.score,
                    quality_reasons=scraper.low_quality_reasons,
                    error=error,
                    # Only the first subtitle is charged for the shared
                    # extraction; the rest genuinely cost what they cost.
                    duration_seconds=(time.monotonic() - started)
                    + (extraction_seconds if index == 0 else 0.0),
                    shared_extraction=index > 0,
                )
            except Exception as exc:
                channel.emit(
                    "result",
                    job_id=str(item["id"]),
                    status="failed",
                    return_code=1,
                    error=str(exc) or type(exc).__name__,
                    duration_seconds=time.monotonic() - started,
                )
        return 0
    except Exception as exc:
        fail_all(str(exc) or type(exc).__name__)
        return 1
    finally:
        logger.removeHandler(scraper)
        scraper.close()


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def main(argv: list[str] | None = None) -> int:
    del argv  # The spec arrives on stdin, not the command line.
    channel = EventChannel(claim_stdout())
    try:
        spec = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError) as exc:
        channel.emit("fatal", error=f"Bad job spec: {exc}")
        return 2
    if not isinstance(spec, dict):
        channel.emit("fatal", error="Job spec must be an object")
        return 2
    return run_group(spec, channel)


if __name__ == "__main__":
    raise SystemExit(main())
