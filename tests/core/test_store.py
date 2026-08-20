"""SQLite persistence for the queue and history."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ffsubsyncui.core.models import JobStatus, SyncJob, SyncOptions, SyncResult
from ffsubsyncui.core.store import JobStore


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
    job = make_job("A", options=SyncOptions(vad="auditok", split_penalty=5.0))
    store.save_jobs([job])
    restored = store.load_jobs()[0]
    assert restored.options.vad == "auditok"
    assert restored.options.split_penalty == 5.0


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
    original = make_job("A", options=SyncOptions(vad="auditok"))
    store.record(original, SyncResult(status=JobStatus.FAILED, error="boom"))
    requeued = store.history(limit=1)[0].to_job()
    assert requeued.reference_path == original.reference_path
    assert requeued.subtitle_path == original.subtitle_path
    assert requeued.options.vad == "auditok"
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
