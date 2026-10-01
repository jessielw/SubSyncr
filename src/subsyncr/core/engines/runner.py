"""Machinery shared by engines, and the loop that drives the native ones.

ffsubsync can fit a reference once and align a whole group against it. alass,
lapse and autosubsync cannot: each subtitle is a separate invocation. They still
run inside one worker process per group, so a five-language episode pays for
process startup once rather than five times, and one cancel stops the lot.

Everything a native engine has in common lives here - resolving or downloading
the binary, writing through a temporary file, backups, progress scraping - so an
engine module is only its argv and how to read its result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from subsyncr.core import binaries, engines, subtitles
from subsyncr.core.engines.base import EngineId
from subsyncr.core.models import FileOptions
from subsyncr.core.output import OutputTransaction
from subsyncr.core.spawn import creation_flags
from subsyncr.core.worker import CancelToken, EventChannel

_log = logging.getLogger(__name__)

# "42.5 %" in a progress bar, whatever else the line carries.
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")

# CSI escapes, and the bare bracket sequences some tools emit without the ESC.
_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


@dataclass(frozen=True, slots=True)
class Invocation:
    """One run of a native tool."""

    argv: list[str]
    # Merged over the inherited environment, not replacing it.
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Outcome:
    """What a run produced, in the vocabulary of ``SyncResult``."""

    status: str
    error: str = ""
    offset_seconds: float | None = None
    framerate_scale_factor: float | None = None
    score: float | None = None
    quality_reasons: list[str] = field(default_factory=list)


# (executable, options, reference, subtitle, output) -> Invocation
InvokeFn = Callable[[Path, dict[str, Any], str, Path, Path], Invocation]
# (returncode, combined output, output path) -> Outcome
InterpretFn = Callable[[int, str, Path], Outcome]
# (reference, subtitle, staged output, options, job id) -> Outcome
RunOneFn = Callable[[str, Path, Path, dict[str, Any], str], Outcome]


def clean_line(line: str) -> str:
    return _ANSI.sub("", line).strip()


def percent_in(line: str) -> float | None:
    """The completion fraction a progress line carries, if any."""
    match = _PERCENT.search(line)
    if match is None:
        return None
    try:
        return max(0.0, min(1.0, float(match.group(1)) / 100.0))
    except ValueError:
        return None


def resolve_binary(engine: EngineId, channel: EventChannel) -> tuple[Path | None, str]:
    """The engine's executable, downloading it if this is the first run."""
    spec = binaries.spec_for(engine)
    if spec is None:
        return None, f"{engine} needs no native binary"
    from subsyncr.core.config import cache_dir

    cache_root = cache_dir()
    if existing := binaries.resolve(spec, cache_root):
        return existing, ""

    status = binaries.status(spec, cache_root)
    if not status.can_download:
        return None, status.message

    channel.emit(
        "log",
        job_id=None,
        level="INFO",
        message=f"downloading {spec.command} {spec.version} from {spec.homepage}",
    )
    last = -1.0

    def on_progress(received: int, total: int | None) -> None:
        nonlocal last
        if not total:
            return
        fraction = received / total
        if fraction - last < 0.02:
            return
        last = fraction
        channel.emit(
            "progress",
            job_id=None,
            fraction=fraction,
            stage=f"Downloading {spec.command}",
        )

    try:
        executable = binaries.ensure(spec, cache_root, on_progress=on_progress)
    except binaries.BinaryError as exc:
        return None, str(exc)
    channel.emit("log", job_id=None, level="INFO", message=f"installed {spec.command}")
    return executable, ""


def run_each(
    spec: dict[str, Any],
    channel: EventChannel,
    cancel: CancelToken,
    *,
    engine: EngineId,
    run_one: RunOneFn,
) -> int:
    """Sync every subtitle in the group, one at a time.

    The shape every engine except ffsubsync has. Backups, staging, cancellation
    and result reporting live here; ``run_one`` only has to align one file and
    say how it went.
    """
    items: list[dict[str, Any]] = list(spec.get("items") or [])
    if not items:
        return 0
    options: dict[str, Any] = spec.get("options") or {}
    files: dict[str, Any] = options.get("files") or {}
    file_options = FileOptions.from_dict(files)
    reference = str(spec["reference"])

    for item in items:
        job_id = str(item["id"])
        if cancel.cancelled:
            _report_cancelled(channel, job_id)
            continue
        started = time.monotonic()
        subtitle = Path(str(item["subtitle"]))
        output = Path(str(item["output"]))
        channel.emit("progress", job_id=job_id, fraction=None, stage="Aligning")

        plan: subtitles.Prepared | None = None
        try:
            with OutputTransaction(subtitle, output, file_options) as transaction:
                staging = transaction.staging
                # Anything this engine cannot read is converted first, and the
                # result converted back, so its format limits never become the
                # user's. Usually a no-op.
                spec_for_engine = engines.get(engine)
                plan = subtitles.prepare(
                    reference,
                    subtitle,
                    staging,
                    accepted=spec_for_engine.input_suffixes,
                    writable=spec_for_engine.output_suffixes,
                    workdir=staging.parent,
                    encoding=str(files.get("encoding", "infer")),
                    output_encoding=str(files.get("output_encoding", "utf-8")),
                    reference_is_subtitle=_is_subtitle(reference),
                )
                outcome = run_one(
                    plan.reference, plan.subtitle, plan.output, options, job_id
                )
                if outcome.status == "succeeded" and not cancel.cancelled:
                    if plan.output.is_file():
                        subtitles.finish(
                            plan,
                            output_encoding=str(files.get("output_encoding", "utf-8")),
                        )
                        transaction.commit()
                    else:
                        outcome.status = "failed"
                        outcome.error = (
                            outcome.error
                            or f"{engine} reported success but wrote nothing"
                        )
                # Anything else leaves the staged file behind unpromoted, so an
                # alignment the engine does not trust never reaches the user's
                # subtitle. The original stays exactly as it was.
            channel.emit(
                "result",
                job_id=job_id,
                status="cancelled" if cancel.cancelled else outcome.status,
                return_code=0 if outcome.status == "succeeded" else 1,
                offset_seconds=outcome.offset_seconds,
                framerate_scale_factor=outcome.framerate_scale_factor,
                score=outcome.score,
                quality_reasons=outcome.quality_reasons,
                error=outcome.error,
                duration_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            channel.emit(
                "result",
                job_id=job_id,
                status="failed",
                return_code=1,
                error=str(exc) or type(exc).__name__,
                duration_seconds=time.monotonic() - started,
            )
        finally:
            if plan is not None:
                plan.clean()
    return 0


def _is_subtitle(reference: str) -> bool:
    from subsyncr.core.scanner import SUBTITLE_EXTENSIONS

    return Path(reference).suffix.lower() in SUBTITLE_EXTENSIONS


def _report_cancelled(channel: EventChannel, job_id: str) -> None:
    channel.emit(
        "result",
        job_id=job_id,
        status="cancelled",
        return_code=1,
        error="Cancelled",
        duration_seconds=0.0,
    )


def run_pairwise(
    spec: dict[str, Any],
    channel: EventChannel,
    cancel: CancelToken,
    *,
    engine: EngineId,
    invoke: InvokeFn,
    interpret: InterpretFn,
) -> int:
    """Run one native tool once per subtitle in the group."""
    executable, problem = resolve_binary(engine, channel)
    if executable is None:
        for item in spec.get("items") or []:
            channel.emit(
                "result",
                job_id=item["id"],
                status="failed",
                return_code=1,
                error=problem,
                duration_seconds=0.0,
            )
        return 1

    def run_one(
        reference: str,
        subtitle: Path,
        staging: Path,
        options: dict[str, Any],
        job_id: str,
    ) -> Outcome:
        invocation = invoke(executable, options, reference, subtitle, staging)
        code, text = _stream(invocation, channel, cancel, job_id=job_id)
        return interpret(code, text, staging)

    return run_each(spec, channel, cancel, engine=engine, run_one=run_one)


def _stream(
    invocation: Invocation,
    channel: EventChannel,
    cancel: CancelToken,
    *,
    job_id: str,
) -> tuple[int, str]:
    """Run the tool, forwarding its output as it arrives.

    stderr is merged into stdout: these tools use both interchangeably, and the
    interleaving is what makes the log readable. Universal newlines split on a
    bare carriage return too, so a progress bar that rewrites one line arrives
    as a series of lines we can read percentages out of.
    """
    environment = dict(os.environ)
    environment.update(invocation.env)
    process = subprocess.Popen(
        invocation.argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=creation_flags(),
    )
    cancel.attach(process)
    collected: list[str] = []
    last = -1.0
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            text = clean_line(raw)
            if not text:
                continue
            collected.append(text)
            fraction = percent_in(text)
            if fraction is not None:
                # Progress bars redraw constantly; only forward real movement.
                if fraction - last >= 0.01:
                    last = fraction
                    channel.emit(
                        "progress",
                        job_id=job_id,
                        fraction=fraction,
                        stage="Aligning",
                    )
                continue
            channel.emit("log", job_id=job_id, level="INFO", message=text)
        return process.wait(), "\n".join(collected)
    finally:
        cancel.detach()
