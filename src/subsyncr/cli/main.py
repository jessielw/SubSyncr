"""Headless frontend over ``subsyncr.core``.

Useful in its own right for scripted batches, and it keeps the core boundary
honest: anything the CLI cannot reach is logic that leaked into the GUI.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import importlib
import json
from pathlib import Path
import sys
import threading

from subsyncr.core import binaries, config as app_config, engines
from subsyncr.core.config import AppConfig
from subsyncr.core.engines import EngineId
from subsyncr.core.ffmpeg import probe
from subsyncr.core.matcher import DEFAULT_THRESHOLD, propose_matches
from subsyncr.core.models import JobStatus, SyncJob, SyncOptions, default_output_for
from subsyncr.core.planner import build_job, jobs_from_matches
from subsyncr.core.queue import QueueCallbacks, SyncQueue
from subsyncr.core.scanner import scan_references, scan_subtitles
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.core.store import JobStore

COMMANDS = ("match", "batch", "sync", "history", "doctor")

_STATUS_SYMBOL = {
    JobStatus.SUCCEEDED: "ok",
    JobStatus.FAILED: "FAIL",
    JobStatus.CANCELLED: "cancelled",
    JobStatus.SKIPPED_LOW_QUALITY: "skipped",
}


def _echo(message: str = "") -> None:
    print(message, file=sys.stdout, flush=True)


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr, flush=True)


def _resolve_folders(video_dir: Path, subtitle_dir: Path | None) -> tuple[Path, Path]:
    return video_dir, subtitle_dir if subtitle_dir is not None else video_dir


def _collect(
    video_dir: Path,
    subtitle_dir: Path,
    *,
    recursive: bool,
    subtitles_as_reference: bool = False,
) -> tuple[list[Path], list[Path]]:
    references = scan_references(
        video_dir, subtitles_as_reference=subtitles_as_reference, recursive=recursive
    )
    subtitles = scan_subtitles(subtitle_dir, recursive=recursive)
    # A file can never be its own reference, which is easy to hit when both
    # sides are subtitles in the same tree.
    chosen = set(references)
    return references, [path for path in subtitles if path not in chosen]


# -- commands ------------------------------------------------------------


def _apply_engine_flags(options: SyncOptions, args: argparse.Namespace) -> str | None:
    """Fold --engine and --fallback into the options. Returns a problem, or None."""
    if chosen := getattr(args, "engine", None):
        options.engine = EngineId(str(chosen))
    raw = getattr(args, "fallback", None)
    if raw is None:
        return None
    chain: list[EngineId] = []
    for name in (part.strip() for part in str(raw).split(",")):
        if not name:
            continue
        if not engines.registered(name):
            known = ", ".join(spec.id.value for spec in engines.all_specs())
            return f"unknown engine {name!r} in --fallback (known: {known})"
        engine = EngineId(name)
        if engine is not options.engine and engine not in chain:
            chain.append(engine)
    options.fallback_chain = tuple(chain)
    return None


def command_match(args: argparse.Namespace) -> int:
    video_dir, subtitle_dir = _resolve_folders(args.video_dir, args.subtitle_dir)
    if not video_dir.is_dir():
        _error(f"not a directory: {video_dir}")
        return 2
    if not subtitle_dir.is_dir():
        _error(f"not a directory: {subtitle_dir}")
        return 2

    videos, subtitles = _collect(
        video_dir,
        subtitle_dir,
        recursive=not args.no_recursive,
        subtitles_as_reference=args.subs_as_reference,
    )
    plan = propose_matches(videos, subtitles, threshold=args.threshold)

    if args.json:
        _echo(
            json.dumps(
                {
                    "matches": [
                        {
                            "video": str(match.video),
                            "subtitle": (
                                str(match.subtitle) if match.subtitle else None
                            ),
                            "confidence": match.confidence,
                            "method": match.method.value,
                            "language": match.language,
                            "reasons": list(match.reasons),
                        }
                        for match in plan.matches
                    ],
                    "unmatched_subtitles": [
                        str(path) for path in plan.unmatched_subtitles
                    ],
                },
                indent=2,
            )
        )
        return 0

    kind = "subtitle references" if args.subs_as_reference else "video files"
    _echo(f"{len(videos)} {kind}, {len(subtitles)} subtitle files")
    _echo()
    for match in plan.matches:
        subtitle = match.subtitle.name if match.subtitle else "(no match)"
        language = f" [{match.language}]" if match.language else ""
        _echo(f"{match.video.name}")
        _echo(
            f"    -> {subtitle}{language}"
            f"  ({match.method.value}, {match.confidence:.0f}%)"
        )
        for reason in match.reasons:
            _echo(f"       - {reason}")
    if plan.unmatched_subtitles:
        _echo()
        _echo(f"{len(plan.unmatched_subtitles)} subtitles with no video:")
        for path in plan.unmatched_subtitles:
            _echo(f"    {path.name}")

    unmatched = len(plan.unmatched_videos)
    _echo()
    _echo(f"{len(plan.matched)} pairings proposed, {unmatched} videos unmatched")
    return 1 if unmatched and args.strict else 0


def _run_jobs(jobs: list[SyncJob], config: AppConfig, *, verbose: bool) -> int:
    """Drain a job list through the real queue and report as it goes."""
    store = JobStore(app_config.database_path())
    cache = SpeechCache(
        app_config.speech_cache_dir(), enabled=config.speech_cache_enabled
    )
    finished = threading.Event()
    seen: set[str] = set()

    def on_job_changed(job: SyncJob) -> None:
        if job.status is JobStatus.RUNNING or job.id in seen:
            return
        if not job.status.is_terminal:
            return
        seen.add(job.id)
        label = _STATUS_SYMBOL.get(job.status, job.status.value)
        detail = ""
        if job.result and job.result.offset_seconds is not None:
            detail = f"  offset {job.result.offset_seconds:+.3f}s"
            if job.result.shared_extraction:
                detail += " (shared extraction)"
            elif job.result.used_speech_cache:
                detail += " (cached speech)"
        _echo(f"[{label}] {job.subtitle_path.name}{detail}")
        if job.last_error:
            _echo(f"         {job.last_error}")

    def on_running_changed(running: bool) -> None:
        if not running:
            finished.set()

    def on_log(job_id: str, level: str, message: str) -> None:
        del job_id
        if verbose:
            _echo(f"    {level}: {message}")

    queue = SyncQueue(
        store,
        config,
        cache,
        QueueCallbacks(
            job_changed=on_job_changed,
            running_changed=on_running_changed,
            log=on_log,
        ),
        # Never clobber the queue the GUI may have stored.
        persist_queue=False,
    )
    queue.add(jobs)
    queue.start()

    try:
        while not finished.wait(timeout=0.25):
            if not queue.is_running and not queue.counts().ready:
                break
    except KeyboardInterrupt:
        _echo()
        _echo("cancelling...")
        queue.cancel_all()
        queue.shutdown()
        store.close()
        return 130

    queue.shutdown()
    counts = queue.counts()
    store.prune_history(config.history_limit)
    store.close()

    _echo()
    _echo(
        f"{counts.succeeded} succeeded, {counts.failed} failed, "
        f"{counts.skipped} skipped, {counts.cancelled} cancelled"
    )
    return 0 if counts.failed == 0 and counts.cancelled == 0 else 1


def command_batch(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    if args.workers:
        config.concurrency = args.workers
        config.normalized()
    if args.fast:
        config.sync.ffsubsync.multi_segment_sync = True
    if (problem := _apply_engine_flags(config.sync, args)) is not None:
        _error(problem)
        return 2

    video_dir, subtitle_dir = _resolve_folders(args.video_dir, args.subtitle_dir)
    if not video_dir.is_dir() or not subtitle_dir.is_dir():
        _error("both paths must be directories")
        return 2

    videos, subtitles = _collect(
        video_dir,
        subtitle_dir,
        recursive=not args.no_recursive,
        subtitles_as_reference=args.subs_as_reference,
    )
    plan = propose_matches(videos, subtitles, threshold=args.threshold)
    jobs = jobs_from_matches(
        plan.matches,
        config.sync,
        output_suffix=config.output_suffix,
        min_confidence=args.auto_match,
    )

    for match in plan.matches:
        if match.subtitle is None:
            _echo(f"[skip] {match.video.name}: no subtitle matched")
        elif match.confidence < args.auto_match:
            _echo(
                f"[skip] {match.video.name} -> {match.subtitle.name}: "
                f"confidence {match.confidence:.0f}% below {args.auto_match:.0f}%"
            )

    if not jobs:
        _echo("nothing to do")
        return 0

    _echo(f"{len(jobs)} jobs queued at concurrency {config.concurrency}")
    if args.dry_run:
        for job in jobs:
            _echo(f"    {job.subtitle_path.name} -> {job.output_path.name}")
        return 0

    _echo()
    return _run_jobs(jobs, config, verbose=args.verbose)


def command_sync(args: argparse.Namespace) -> int:
    config = AppConfig.load()
    options: SyncOptions = config.sync
    if not args.video.is_file():
        _error(f"reference not found: {args.video}")
        return 2
    if not args.subtitle.is_file():
        _error(f"subtitle not found: {args.subtitle}")
        return 2

    output = args.output or default_output_for(args.subtitle, config.output_suffix)
    job = build_job(
        args.video,
        args.subtitle,
        options,
        output_suffix=config.output_suffix,
        # A single job has nobody to share a speech array with.
        group_key="",
    )
    job.output_path = output
    if args.overwrite:
        job.options.files.overwrite_input = True
    if args.replace:
        job.options.files.replace_output = True
    if args.fast:
        job.options.ffsubsync.multi_segment_sync = True
    if (problem := _apply_engine_flags(job.options, args)) is not None:
        _error(problem)
        return 2

    _echo(f"{args.subtitle.name} -> {output.name}")
    return _run_jobs([job], config, verbose=args.verbose)


def command_history(args: argparse.Namespace) -> int:
    store = JobStore(app_config.database_path())
    try:
        entries = store.history(limit=args.limit, search=args.search or "")
        if args.json:
            _echo(
                json.dumps(
                    [
                        {
                            "finished_at": entry.finished_at,
                            "status": entry.status.value,
                            "subtitle": str(entry.subtitle_path),
                            "reference": str(entry.reference_path),
                            "offset_seconds": entry.offset_seconds,
                            "score": entry.score,
                            "duration_seconds": entry.duration_seconds,
                            "error": entry.error,
                        }
                        for entry in entries
                    ],
                    indent=2,
                )
            )
            return 0
        if not entries:
            _echo("no history yet")
            return 0
        for entry in entries:
            offset = (
                f"{entry.offset_seconds:+.3f}s"
                if entry.offset_seconds is not None
                else "-"
            )
            _echo(
                f"{entry.status.value:20} {offset:>10}  {entry.subtitle_path.name}"
                + (f"  ({entry.error})" if entry.error else "")
            )
        return 0
    finally:
        store.close()


def command_doctor(args: argparse.Namespace) -> int:
    del args
    config = AppConfig.load()
    status = probe(config.sync.environment.ffmpeg_path)
    _echo(f"config      : {app_config.config_path()}")
    _echo(f"database    : {app_config.database_path()}")
    _echo(f"speech cache: {app_config.speech_cache_dir()}")
    _echo(f"concurrency : {config.concurrency}")
    _echo(f"ffmpeg      : {status.message}")
    if not status.has_ffprobe and status.ready:
        _echo("              progress will be indeterminate without ffprobe")
    _echo(f"engine      : {config.sync.engine}")
    if config.sync.fallback_chain:
        _echo(f"fallback    : {', '.join(config.sync.fallback_chain)}")
    _echo("")
    _echo("engines:")
    healthy = True
    for spec in engines.all_specs():
        _echo(f"  {spec.label:<12} {_engine_report(spec)}")
        if spec.id is config.sync.engine and "unavailable" in _engine_report(spec):
            healthy = False
    return 0 if status.ready and healthy else 1


def _engine_report(spec: engines.EngineSpec) -> str:
    """One line on whether this engine could run right now."""
    if spec.kind is engines.EngineKind.BINARY:
        binary = binaries.spec_for(spec.id)
        if binary is None:  # pragma: no cover - a registry mistake
            return "no binary configured"
        return binaries.status(binary, app_config.cache_dir()).message
    try:
        module = importlib.import_module(
            "ffsubsync" if spec.id is EngineId.FFSUBSYNC else "autosubsync"
        )
    except Exception as exc:
        return f"unavailable ({exc})"
    version = getattr(module, "__version__", "installed")
    return f"ready ({version})"


# -- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subsyncr",
        description="Batch subtitle synchronization for loose videos and subtitles.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_engine_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--engine",
            choices=[spec.id.value for spec in engines.all_specs()],
            default=None,
            help="which aligner to use (default: whatever Settings says)",
        )
        target.add_argument(
            "--fallback",
            default=None,
            metavar="ENGINE,...",
            help=(
                "engines to retry with, in order, when the first one fails or "
                "returns a low-quality verdict"
            ),
        )

    def add_scan_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("video_dir", type=Path)
        target.add_argument("subtitle_dir", type=Path, nargs="?", default=None)
        target.add_argument("--no-recursive", action="store_true")
        target.add_argument(
            "--subs-as-reference",
            action="store_true",
            help="treat the reference folder as subtitles rather than video; "
            "needs no ffmpeg and is far faster",
        )
        target.add_argument(
            "--threshold",
            type=float,
            default=DEFAULT_THRESHOLD,
            help="fuzzy similarity floor for a pairing (default: %(default)s)",
        )

    match = subparsers.add_parser("match", help="propose pairings without syncing")
    add_scan_flags(match)
    match.add_argument("--json", action="store_true")
    match.add_argument(
        "--strict", action="store_true", help="exit non-zero if any video is unmatched"
    )
    match.set_defaults(handler=command_match)

    batch = subparsers.add_parser("batch", help="match a folder pair, then sync")
    add_scan_flags(batch)
    batch.add_argument(
        "--auto-match",
        type=float,
        default=90.0,
        metavar="PERCENT",
        help="only queue pairings at or above this confidence (default: %(default)s)",
    )
    batch.add_argument("--workers", type=int, default=0, metavar="N")
    add_engine_flags(batch)
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument(
        "--fast",
        action="store_true",
        help="sample short segments instead of reading each reference in full",
    )
    batch.add_argument("-v", "--verbose", action="store_true")
    batch.set_defaults(handler=command_batch)

    sync = subparsers.add_parser("sync", help="sync one subtitle against one reference")
    sync.add_argument("video", type=Path)
    sync.add_argument("subtitle", type=Path)
    sync.add_argument("-o", "--output", type=Path, default=None)
    sync.add_argument("--overwrite", action="store_true", help="write over the input")
    sync.add_argument(
        "--replace", action="store_true", help="allow replacing an existing output"
    )
    sync.add_argument(
        "--fast",
        action="store_true",
        help="sample short segments instead of reading the reference in full",
    )
    add_engine_flags(sync)
    sync.add_argument("-v", "--verbose", action="store_true")
    sync.set_defaults(handler=command_sync)

    history = subparsers.add_parser("history", help="show completed jobs")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--search", default="")
    history.add_argument("--json", action="store_true")
    history.set_defaults(handler=command_history)

    doctor = subparsers.add_parser("doctor", help="report environment and paths")
    doctor.set_defaults(handler=command_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
