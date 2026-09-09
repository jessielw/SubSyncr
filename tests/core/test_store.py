"""SQLite persistence for the queue and history."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sqlite3

from support import sync_options

from subsyncr.core.engines import EngineId
from subsyncr.core.models import JobStatus, SyncJob, SyncResult
from subsyncr.core.store import SCHEMA_VERSION, JobStore


def test_queue_round_trip(store: JobStore, make_job: Callable[..., SyncJob]) -> None:
    jobs = [make_job("A"), make_job("B")]
    store.save_jobs(jobs)
    restored = store.load_jobs()
    assert [job.id for job in restored] == [job.id for job in jobs]
    assert restored[0].reference_path == jobs[0].reference_path


def test_queue_order_is_preserved(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    jobs = [make_job(f"Show{i:02d}") for i in range(8)]
    store.save_jobs(jobs)
    assert [job.id for job in store.load_jobs()] == [job.id for job in jobs]


def test_options_survive_the_round_trip(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("A", options=sync_options(vad="auditok", split_penalty=5.0))
    store.save_jobs([job])
    restored = store.load_jobs()[0]
    assert restored.options.ffsubsync.vad == "auditok"
    assert restored.options.ffsubsync.split_penalty == 5.0


def test_succeeded_jobs_are_not_stored_as_pending(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    done = make_job("A")
    done.status = JobStatus.SUCCEEDED
    store.save_jobs([done, make_job("B")])
    assert len(store.load_jobs()) == 1


def test_a_running_job_comes_back_queued(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    """Crash recovery: we cannot have been running while the app was closed."""
    job = make_job("A")
    job.status = JobStatus.RUNNING
    store.save_jobs([job])
    restored = store.load_jobs()[0]
    assert restored.status is JobStatus.QUEUED
    assert "Interrupted" in restored.last_error


def test_history_records_the_numbers_worth_keeping(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("A")
    store.record(
        job,
        SyncResult(
            status=JobStatus.SUCCEEDED,
            offset_seconds=-2.5,
            framerate_scale_factor=1.001,
            score=42.0,
            duration_seconds=3.5,
            used_speech_cache=True,
        ),
    )
    entry = store.history(limit=5)[0]
    assert entry.status is JobStatus.SUCCEEDED
    assert entry.offset_seconds == -2.5
    assert entry.score == 42.0
    assert entry.used_speech_cache is True


def test_history_entry_rebuilds_a_queueable_job(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    original = make_job("A", options=sync_options(vad="auditok"))
    store.record(original, SyncResult(status=JobStatus.FAILED, error="boom"))
    requeued = store.history(limit=1)[0].to_job()
    assert requeued.reference_path == original.reference_path
    assert requeued.subtitle_path == original.subtitle_path
    assert requeued.options.ffsubsync.vad == "auditok"
    assert requeued.status is JobStatus.QUEUED


def test_history_search_filters_by_name(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    store.record(make_job("Alpha"), SyncResult(status=JobStatus.SUCCEEDED))
    store.record(make_job("Beta"), SyncResult(status=JobStatus.SUCCEEDED))
    assert len(store.history(search="Alpha")) == 1
    assert len(store.history(search="")) == 2


def test_history_filters_by_status(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    store.record(make_job("A"), SyncResult(status=JobStatus.SUCCEEDED))
    store.record(make_job("B"), SyncResult(status=JobStatus.FAILED))
    failed = store.history(statuses=(JobStatus.FAILED,))
    assert len(failed) == 1
    assert failed[0].status is JobStatus.FAILED


def test_history_is_newest_first(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    first, second = make_job("First"), make_job("Second")
    store.record(first, SyncResult(status=JobStatus.SUCCEEDED))
    store.record(second, SyncResult(status=JobStatus.SUCCEEDED))
    assert store.history(limit=2)[0].id == second.id


def test_prune_keeps_the_newest(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    for index in range(10):
        store.record(make_job(f"Show{index}"), SyncResult(status=JobStatus.SUCCEEDED))
    store.prune_history(4)
    assert len(store.history(limit=100)) == 4


def test_prune_of_zero_keeps_everything(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    """Zero means "no limit configured", not "delete it all"."""
    store.record(make_job("A"), SyncResult(status=JobStatus.SUCCEEDED))
    assert store.prune_history(0) == 0
    assert len(store.history()) == 1


def test_clear_history(store: JobStore, make_job: Callable[..., SyncJob]) -> None:
    store.record(make_job("A"), SyncResult(status=JobStatus.SUCCEEDED))
    assert store.clear_history() == 1
    assert store.history() == []


def test_reopening_the_database_keeps_the_queue(
    tmp_path: Path, make_job: Callable[..., SyncJob]
) -> None:
    path = tmp_path / "jobs.db"
    first = JobStore(path)
    job = make_job("A")
    first.save_jobs([job])
    first.close()

    second = JobStore(path)
    try:
        assert [item.id for item in second.load_jobs()] == [job.id]
    finally:
        second.close()


def test_corrupt_row_does_not_lose_the_whole_queue(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    good = make_job("A")
    store.save_jobs([good])
    # Reach past the API on purpose: this is about surviving a bad row.
    store._connection.execute(
        "INSERT INTO jobs (id, position, enabled, status, reference_path,"
        " subtitle_path, output_path, options, last_error, created_at)"
        " VALUES ('bad', 1, 1, 'nonsense', 'a', 'b', 'c', 'not json', '', 0.0)"
    )
    store._connection.commit()
    restored = store.load_jobs()
    assert [job.id for job in restored] == [good.id]


# -- schema migration ----------------------------------------------------


def _v1_database(path: Path) -> None:
    """A database exactly as schema version 1 left it: no engine columns."""
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, position INTEGER NOT NULL,
            enabled INTEGER NOT NULL, status TEXT NOT NULL,
            reference_path TEXT NOT NULL, subtitle_path TEXT NOT NULL,
            output_path TEXT NOT NULL, options TEXT NOT NULL, language TEXT,
            group_key TEXT, last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE TABLE history (
            id TEXT PRIMARY KEY, finished_at REAL NOT NULL, status TEXT NOT NULL,
            reference_path TEXT NOT NULL, subtitle_path TEXT NOT NULL,
            output_path TEXT NOT NULL, options TEXT NOT NULL, language TEXT,
            offset_seconds REAL, framerate_scale_factor REAL, score REAL,
            duration_seconds REAL, used_speech_cache INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        );
        PRAGMA user_version = 1;
        """
    )
    # A row written by the old app: flat options, no engine recorded.
    connection.execute(
        "INSERT INTO history (id, finished_at, status, reference_path,"
        " subtitle_path, output_path, options, language, used_speech_cache, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old-1",
            1.0,
            JobStatus.SUCCEEDED.value,
            "ep.mkv",
            "ep.srt",
            "ep.synced.srt",
            json.dumps({"vad": "auditok", "overwrite_input": True}),
            "en",
            0,
            "",
        ),
    )
    connection.commit()
    connection.close()


def test_a_version_1_database_upgrades_in_place(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    _v1_database(path)
    store = JobStore(path)
    try:
        version = store._connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        entry = store.history(limit=1)[0]
        # The old flat options are lifted, and the missing engine column
        # defaults to the only engine that build could have used.
        assert entry.options.ffsubsync.vad == "auditok"
        assert entry.options.files.overwrite_input is True
        assert entry.engine is EngineId.FFSUBSYNC
    finally:
        store.close()


def test_the_engine_that_ran_is_recorded_not_the_one_configured(
    store: JobStore, make_job: Callable[..., SyncJob]
) -> None:
    """A fallback chain can finish a job on an engine it did not start on."""
    job = make_job("A")
    store.record(
        job,
        SyncResult(status=JobStatus.SUCCEEDED, engine=EngineId.ALASS),
    )
    entry = store.history(limit=1)[0]
    assert entry.options.engine is EngineId.FFSUBSYNC
    assert entry.engine is EngineId.ALASS
