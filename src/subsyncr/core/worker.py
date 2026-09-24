"""Child process that syncs one reference against one or more subtitles.

Run as ``python -m subsyncr.core.worker``, or as ``<frozen exe> --sync-worker``
when packaged. Reads a JSON job-group spec on stdin and writes newline-delimited
JSON events on stdout.

This module owns the *protocol* - the event channel, stdout ownership, and
cancellation. It does not know how any particular engine works: it reads the
spec's ``engine`` field, imports that engine's module by the dotted path in the
registry, and hands over. Engines are imported here and nowhere else, so the
GUI process never pays for numpy, ffsubsync or anything else they drag in.

Isolating an engine in its own process buys three things that are impossible
in-process:

* **Cancellation.** ffsubsync exposes no cancel hook, but a process can be killed.
* **Memory.** Everything a sync allocated is returned to the OS when it exits.
* **Root logger safety.** ``import ffsubsync`` calls ``logging.basicConfig()``,
  which would otherwise reconfigure the host application's logging.

A *group* is one reference plus every subtitle that syncs against it, usually
the language variants of one episode. For ffsubsync the whole group shares a
single extraction held in memory - about fifteen times faster per sibling than
paying process startup and a serialized speech array for each one. Engines that
cannot share simply loop, and still save the process startup per subtitle.

**Cancellation.** The parent writes the spec, then holds stdin open. Closing it
is the cancel signal: a watcher thread sees EOF, sets the token, and terminates
whatever native child is running. This works identically on Windows and POSIX,
where signals and process groups do not. The parent still kills the worker
outright if it does not exit in time. Because a pipe held open forever would
hang any tool that reads stdin, ``claim_stdin`` moves the real pipe to a private
descriptor and leaves fd 0 pointing at the null device for children to inherit.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any, TextIO

from subsyncr.core import engines
from subsyncr.core.engines import EngineId
from subsyncr.core.spawn import ConsoleGuard


class EventChannel:
    """Newline-delimited JSON writer over a private copy of stdout."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, event: str, **payload: Any) -> None:
        payload["event"] = event
        line = json.dumps(payload, default=str) + "\n"
        try:
            # Engines may report from a reader thread while the main thread
            # emits progress; a half-written line would break the protocol.
            with self._lock:
                self._stream.write(line)
                self._stream.flush()
        except (OSError, ValueError):
            # The parent went away. Nothing useful left to do.
            pass


class CancelToken:
    """A cancellation flag that also reaps whatever native child is running.

    Engines that spawn a binary register it with ``attach`` so a cancel arriving
    mid-run does not have to wait for the tool to finish on its own.
    """

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._lock = threading.Lock()
        self._child: subprocess.Popen[Any] | None = None

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def cancel(self) -> None:
        self._flag.set()
        with self._lock:
            child = self._child
        if child is not None and child.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                child.terminate()

    def attach(self, child: subprocess.Popen[Any]) -> None:
        """Register the running child, terminating it if we are already cancelled."""
        with self._lock:
            self._child = child
        if self.cancelled and child.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                child.terminate()

    def detach(self) -> None:
        with self._lock:
            self._child = None


def claim_stdout() -> TextIO:
    """Take exclusive ownership of fd 1 for the event channel.

    Anything else that writes to stdout - a stray print, ffsubsync writing
    subtitles to ``sys.stdout.fileno()`` when ``-o`` is missing, a native tool
    inheriting our handles - is rerouted to stderr so it can never corrupt the
    protocol.

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


def claim_stdin() -> TextIO:
    """Take fd 0 for the control channel and leave children an empty stdin.

    The parent holds our stdin open so that closing it can mean "cancel", but a
    pipe that never reaches EOF is poison for anything we spawn: ffmpeg reads
    stdin for interactive commands and will sit on it forever. So the real pipe
    is moved to a private descriptor and fd 0 is pointed at the null device,
    which every child then inherits and sees as an immediate end of input.

    The mirror image of ``claim_stdout``, and for the same reason: the protocol
    owns the standard descriptors, and nothing else gets to touch them.
    """
    saved_fd = os.dup(0)
    empty = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(empty, 0)
    finally:
        os.close(empty)
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115
    return os.fdopen(saved_fd, "r", encoding="utf-8", errors="replace")


def watch_for_cancel(cancel: CancelToken, stream: TextIO) -> threading.Thread:
    """Cancel as soon as the parent closes our stdin.

    Daemon, so a worker that finishes normally is never held open by a thread
    still blocked on a read.
    """

    def wait() -> None:
        with contextlib.suppress(OSError, ValueError):
            # Any further line, or EOF, means stop. The parent sends nothing
            # after the spec, so in practice this returns "" when it hangs up.
            stream.readline()
        cancel.cancel()

    thread = threading.Thread(target=wait, name="cancel-watch", daemon=True)
    thread.start()
    return thread


def use_embedded_reference(
    spec: dict[str, Any], channel: EventChannel, workdir: Path
) -> Path | None:
    """Swap the video reference for a subtitle track inside it, if asked.

    Done here rather than inside each engine because it is a property of the
    group's reference, not of how any one tool aligns. lapse is the exception:
    it searches embedded tracks itself, and doing it twice would only take the
    choice away from it.

    Any failure is logged and ignored - the video is still a perfectly good
    reference, just a slower one.
    """
    options: dict[str, Any] = spec.get("options") or {}
    if not options.get("extract_embedded"):
        return None
    if str(spec.get("engine", "")) == EngineId.LAPSE.value:
        return None

    from subsyncr.core import embedded
    from subsyncr.core.scanner import needs_ffmpeg

    reference = Path(str(spec["reference"]))
    if not needs_ffmpeg(reference):
        return None  # Already a subtitle or a cached speech array.

    environment: dict[str, Any] = options.get("environment") or {}
    items: list[dict[str, Any]] = list(spec.get("items") or [])
    language = str(items[0].get("language", "")) if items else ""
    try:
        found = embedded.reference_from(
            reference,
            workdir,
            prefer_language=language or None,
            ffmpeg_path=str(environment.get("ffmpeg_path", "")),
        )
    except embedded.ExtractionError as exc:
        channel.emit(
            "log",
            job_id=None,
            level="WARNING",
            message=f"no embedded subtitle to align against ({exc}); using the audio",
        )
        return None
    if found is None:
        channel.emit(
            "log",
            job_id=None,
            level="INFO",
            message="no text subtitle track in the reference; using the audio",
        )
        return None

    extracted, track = found
    label = track.language or track.title or f"track {track.index}"
    channel.emit(
        "log",
        job_id=None,
        level="INFO",
        message=f"aligning against the embedded {label} subtitle instead of the audio",
    )
    spec["reference"] = str(extracted)
    return extracted


def use_pgs_reference(spec: dict[str, Any], channel: EventChannel) -> None:
    """Resolve ffsubsync's "auto" PGS track, or fall back to the audio.

    Left to itself, ffsubsync takes the first PGS track and fails every job in
    the group when there is none. Resolving it here picks the track by
    language, skips forced ones, and turns a missing track into a slower sync
    rather than a failed one. An explicit stream is the user's call and is
    passed through untouched.
    """
    if str(spec.get("engine", EngineId.FFSUBSYNC.value)) != EngineId.FFSUBSYNC.value:
        return
    options: dict[str, Any] = spec.get("options") or {}
    tuning: dict[str, Any] = options.get("ffsubsync") or {}
    if str(tuning.get("pgs_ref_stream", "")).strip() != "auto":
        return

    from subsyncr.core import embedded
    from subsyncr.core.scanner import needs_ffmpeg

    if not needs_ffmpeg(Path(str(spec["reference"]))):
        return  # An extracted text track already won; build_arguments skips it.

    environment: dict[str, Any] = options.get("environment") or {}
    items: list[dict[str, Any]] = list(spec.get("items") or [])
    language = str(items[0].get("language", "")) if items else ""
    track = embedded.choose_pgs(
        embedded.probe_tracks(
            Path(str(spec["reference"])), str(environment.get("ffmpeg_path", ""))
        ),
        language or None,
    )
    if track is None:
        tuning["pgs_ref_stream"] = ""
        channel.emit(
            "log",
            job_id=None,
            level="INFO",
            message="no PGS subtitle track in the reference; using the audio",
        )
        return
    tuning["pgs_ref_stream"] = f"s:{track.index}"
    label = track.language or track.title or f"track {track.index}"
    channel.emit(
        "log",
        job_id=None,
        level="INFO",
        message=f"aligning against the {label} PGS subtitle track (s:{track.index})",
    )


def dispatch(spec: dict[str, Any], channel: EventChannel, cancel: CancelToken) -> int:
    """Hand the group to the engine it names."""
    engine = str(spec.get("engine", "ffsubsync"))
    try:
        module = importlib.import_module(engines.get(engine).module)
    except KeyError:
        channel.emit("fatal", error=f"Unknown sync engine: {engine}")
        return 2
    except ImportError as exc:
        channel.emit("fatal", error=f"Sync engine {engine} is unavailable: {exc}")
        return 2
    with tempfile.TemporaryDirectory(prefix="subsyncr-") as scratch:
        use_embedded_reference(spec, channel, Path(scratch))
        use_pgs_reference(spec, channel)
        return int(module.run_group(spec, channel, cancel))


def main(argv: list[str] | None = None) -> int:
    del argv  # The spec arrives on stdin, not the command line.
    # Engines run in this process and shell out to ffmpeg on their own terms;
    # without this a packaged build flashes a console window for every call.
    # Also reached through subsyncr.__main__, but not when this module is run
    # directly as ``python -m subsyncr.core.worker``.
    ConsoleGuard.install()
    channel = EventChannel(claim_stdout())
    cancel = CancelToken()
    if sys.stdin is None:
        channel.emit("fatal", error="No job spec: the worker has no stdin")
        return 2
    control = claim_stdin()
    try:
        # One line, not read-to-EOF: the parent keeps the pipe open so that
        # closing it later can mean "cancel".
        raw = control.readline()
    except (OSError, ValueError) as exc:
        channel.emit("fatal", error=f"Could not read the job spec: {exc}")
        return 2
    if not raw.strip():
        channel.emit("fatal", error="No job spec was sent")
        return 2
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        channel.emit("fatal", error=f"Bad job spec: {exc}")
        return 2
    if not isinstance(spec, dict):
        channel.emit("fatal", error="Job spec must be an object")
        return 2
    watch_for_cancel(cancel, control)
    return dispatch(spec, channel, cancel)


if __name__ == "__main__":
    raise SystemExit(main())
