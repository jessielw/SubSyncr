"""The application log file.

Everything logs through the standard library: a module takes
``logging.getLogger(__name__)``, and because every module lives under the
``subsyncr`` package, every record lands on the one logger configured here.

Nothing is written until ``configure`` runs. The GUI and the CLI call it at
startup; the sync worker never does. The worker is a child process: it calls
``send_records_to`` instead, which turns each record into a ``log`` event on
its event channel, and the parent writes those with ``forward``. So only one
process ever writes the file, and midnight rotation has a single owner.

The file handler sits behind a ``QueueListener``, so a log call from the GUI
thread is a queue put rather than a disk write.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
import logging
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import sys
import threading
from typing import Any, Self

from subsyncr import __version__

ROOT_LOGGER = "subsyncr"
LOG_FILE_NAME = "subsyncr.log"
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 365

_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

# How the parent tells a worker which level to send; see child_environment().
LEVEL_ENV = "SUBSYNCR_LOG_LEVEL"

# The logger forwarded worker lines are written under, so a reader can tell
# them apart from what the parent process said itself.
_WORKER_LOGGER = f"{ROOT_LOGGER}.worker"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def number(self) -> int:
        return logging.getLevelNamesMapping()[self.name]

    @classmethod
    def parse(cls, value: object, default: Self) -> Self:
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return default


_root = logging.getLogger(ROOT_LOGGER)
# Our records are ours alone. ffsubsync calls ``logging.basicConfig()`` when it
# is imported, and a record propagating to that root handler would be printed
# a second time.
_root.propagate = False
# Until configure() runs, drop records rather than let logging's last-resort
# handler print warnings to a stderr a windowed build does not have.
_root.addHandler(logging.NullHandler())
_root.setLevel(logging.INFO)

_lock = threading.Lock()
_listener: QueueListener | None = None
_queue_handler: QueueHandler | None = None
_file_handler: TimedRotatingFileHandler | None = None
_log_file: Path | None = None


def configure(
    log_file: Path | None,
    level: LogLevel = LogLevel.INFO,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    console: bool = False,
) -> None:
    """Start writing records to ``log_file`` (and stderr, if asked).

    Safe to call again: the previous sinks are flushed and replaced, never
    stacked, so a record is written once no matter how often this runs.
    """
    global _listener, _queue_handler, _file_handler, _log_file
    shutdown()
    sinks: list[logging.Handler] = []
    formatter = logging.Formatter(_FORMAT)
    file_handler: TimedRotatingFileHandler | None = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = TimedRotatingFileHandler(
                log_file,
                when="midnight",
                backupCount=_clamp_retention(retention_days),
                encoding="utf-8",
                delay=True,
            )
        except OSError:
            # An unwritable log folder must never stop the app starting.
            file_handler = None
        else:
            file_handler.setFormatter(formatter)
            sinks.append(file_handler)
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        sinks.append(stream)
    if not sinks:
        return

    records: queue.Queue[Any] = queue.Queue()
    handler = QueueHandler(records)
    listener = QueueListener(records, *sinks, respect_handler_level=True)
    listener.start()
    with _lock:
        _root.addHandler(handler)
        _listener = listener
        _queue_handler = handler
        _file_handler = file_handler
        _log_file = log_file if file_handler is not None else None
    set_level(level)
    _root.info(
        "SubSyncr %s on Python %s, %s",
        __version__,
        platform.python_version(),
        platform.platform(),
    )


def set_level(level: LogLevel) -> None:
    _root.setLevel(level.number)


def current_level() -> LogLevel:
    name = logging.getLevelName(_root.getEffectiveLevel()).lower()
    return LogLevel.parse(name, LogLevel.INFO)


def child_environment() -> dict[str, str]:
    """Variables that make a worker log at the level this process is using."""
    return {LEVEL_ENV: current_level().value}


class _CallbackHandler(logging.Handler):
    def __init__(self, send: Callable[[str, str], None]) -> None:
        super().__init__()
        self._send = send
        # The parent adds its own timestamp and level when it writes the line.
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._send(record.levelname, self.format(record))
        except Exception:
            self.handleError(record)


def send_records_to(send: Callable[[str, str], None]) -> None:
    """In a worker process, hand every record to ``send(level, message)``.

    The level comes from ``LEVEL_ENV``, which the parent sets from its own, so
    raising the level in Settings reaches the next worker that starts.
    """
    set_level(LogLevel.parse(os.environ.get(LEVEL_ENV, ""), LogLevel.INFO))
    _root.addHandler(_CallbackHandler(send))


def set_retention(days: int) -> None:
    """How many rotated files to keep. Takes effect at the next rotation."""
    with _lock:
        if _file_handler is not None:
            _file_handler.backupCount = _clamp_retention(days)


def current_log_file() -> Path | None:
    """The file being written, or None when logging to a file is off."""
    with _lock:
        return _log_file


def forward(level: str, message: str) -> None:
    """Write a line the sync worker sent over its event channel.

    The level arrives as a name such as ``"WARNING"``. A name we do not
    recognise is written at INFO rather than dropped.
    """
    number = logging.getLevelNamesMapping().get(str(level).upper(), logging.INFO)
    logging.getLogger(_WORKER_LOGGER).log(number, "%s", message)


def shutdown() -> None:
    """Flush what is queued and close the sinks. Call on application exit."""
    global _listener, _queue_handler, _file_handler, _log_file
    with _lock:
        listener, handler = _listener, _queue_handler
        _listener = _queue_handler = _file_handler = None
        _log_file = None
    if handler is not None:
        _root.removeHandler(handler)
    if listener is not None:
        # stop() drains the queue before joining, so nothing logged is lost.
        listener.stop()
        for sink in listener.handlers:
            sink.close()


def _clamp_retention(days: int) -> int:
    return max(1, min(MAX_RETENTION_DAYS, int(days)))
