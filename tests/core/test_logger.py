"""The log file: what reaches it, at what level, and from which process."""

from __future__ import annotations

from collections.abc import Iterator
import logging
from pathlib import Path

import pytest

from subsyncr.core import logger


@pytest.fixture(autouse=True)
def restored_logger() -> Iterator[None]:
    """Leave the process-wide logger as each test found it."""
    root = logging.getLogger(logger.ROOT_LOGGER)
    handlers, level = list(root.handlers), root.level
    yield
    logger.shutdown()
    root.handlers[:] = handlers
    root.setLevel(level)


def _written(path: Path) -> str:
    logger.shutdown()  # Drains the queue, so everything logged is on disk.
    return path.read_text(encoding="utf-8")


def test_records_reach_the_file(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "subsyncr.log"
    logger.configure(path)
    logging.getLogger("subsyncr.core.queue").warning("something odd")
    text = _written(path)
    assert "SubSyncr" in text.splitlines()[0], "the version line comes first"
    assert "WARNING - subsyncr.core.queue - something odd" in text


def test_the_level_filters(tmp_path: Path) -> None:
    path = tmp_path / "subsyncr.log"
    logger.configure(path, logger.LogLevel.INFO)
    probe = logging.getLogger("subsyncr.test")
    probe.debug("hidden")
    logger.set_level(logger.LogLevel.DEBUG)
    probe.debug("shown")
    text = _written(path)
    assert "hidden" not in text
    assert "shown" in text


def test_configuring_twice_writes_each_line_once(tmp_path: Path) -> None:
    path = tmp_path / "subsyncr.log"
    logger.configure(path)
    logger.configure(path)
    logging.getLogger("subsyncr.test").info("once")
    assert _written(path).count("once") == 1


def test_an_unwritable_folder_does_not_stop_startup(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    logger.configure(blocker / "subsyncr.log")
    assert logger.current_log_file() is None
    logging.getLogger("subsyncr.test").error("dropped, not raised")


def test_forwarded_worker_lines_keep_their_level(tmp_path: Path) -> None:
    path = tmp_path / "subsyncr.log"
    logger.configure(path)
    logger.forward("WARNING", "ep1.srt: ffmpeg complained")
    logger.forward("nonsense", "ep1.srt: still written")
    text = _written(path)
    assert "WARNING - subsyncr.worker - ep1.srt: ffmpeg complained" in text
    assert "INFO - subsyncr.worker - ep1.srt: still written" in text


def test_a_worker_sends_records_at_the_parents_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger.set_level(logger.LogLevel.DEBUG)
    environment = logger.child_environment()
    assert environment == {logger.LEVEL_ENV: "debug"}

    # What the child sees once the parent has launched it.
    logger.set_level(logger.LogLevel.INFO)
    monkeypatch.setenv(logger.LEVEL_ENV, environment[logger.LEVEL_ENV])
    sent: list[tuple[str, str]] = []
    logger.send_records_to(lambda level, message: sent.append((level, message)))
    logging.getLogger("subsyncr.core.embedded").debug("probe failed")
    assert sent == [("DEBUG", "subsyncr.core.embedded: probe failed")]


def test_a_worker_without_a_level_sends_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(logger.LEVEL_ENV, raising=False)
    sent: list[tuple[str, str]] = []
    logger.send_records_to(lambda level, message: sent.append((level, message)))
    probe = logging.getLogger("subsyncr.test")
    probe.debug("quiet")
    probe.info("loud")
    assert [message for _, message in sent] == ["subsyncr.test: loud"]


def test_retention_is_clamped(tmp_path: Path) -> None:
    logger.configure(tmp_path / "subsyncr.log", retention_days=0)
    handler = logger._file_handler
    assert handler is not None and handler.backupCount == 1
    logger.set_retention(10_000)
    assert handler.backupCount == logger.MAX_RETENTION_DAYS
