"""Settling a job's reference track when it is queued, not when it runs."""

from __future__ import annotations

from collections.abc import Callable
import io
import json
from pathlib import Path
import time
from typing import Any, ClassVar

import numpy as np
import pytest

from subsyncr.core import embedded, queue as queue_module, reference
from subsyncr.core.config import AppConfig
from subsyncr.core.embedded import Track
from subsyncr.core.engines import EngineId
from subsyncr.core.models import (
    JobStatus,
    ReferenceKind,
    ReferenceTrack,
    SyncJob,
    SyncOptions,
    SyncResult,
)
from subsyncr.core.queue import SyncQueue
from subsyncr.core.speech_cache import SpeechCache
from subsyncr.core.store import JobStore
from subsyncr.core.worker import EventChannel, use_pgs_reference

PGS = "hdmv_pgs_subtitle"

# The layout of a typical dual-audio remux: an unflagged, default Signs & Songs
# track first, the full dialogue tracks after it.
DUAL_AUDIO = [
    Track(index=0, codec=PGS, language="eng", title="Signs & Songs", default=True),
    Track(index=1, codec=PGS, language="spa", title="Signs & Songs"),
    Track(index=2, codec=PGS, language="eng"),
    Track(index=3, codec=PGS, language="spa"),
]


def _options(**ffsubsync: Any) -> SyncOptions:
    options = SyncOptions()
    for key, value in ffsubsync.items():
        setattr(options.ffsubsync, key, value)
    return options


# -- the rules -----------------------------------------------------------


def test_auto_picks_the_full_track_in_the_subtitles_language() -> None:
    chosen = reference.choose(DUAL_AUDIO, _options(), "es")
    assert chosen == ReferenceTrack(ReferenceKind.PGS, 3, "spa", "")


def test_pgs_off_means_audio() -> None:
    chosen = reference.choose(DUAL_AUDIO, _options(pgs_ref_stream=""), "en")
    assert chosen is not None
    assert chosen.kind is ReferenceKind.AUDIO


def test_no_pgs_track_means_audio() -> None:
    chosen = reference.choose([Track(index=0, codec="subrip")], _options(), "en")
    assert chosen is not None
    assert chosen.kind is ReferenceKind.AUDIO


def test_an_extracted_text_track_wins_when_enabled() -> None:
    options = _options()
    options.extract_embedded = True
    tracks = [*DUAL_AUDIO, Track(index=4, codec="subrip", language="eng")]
    chosen = reference.choose(tracks, options, "en")
    assert chosen is not None
    assert (chosen.kind, chosen.index) == (ReferenceKind.TEXT, 4)


def test_an_explicit_stream_is_kept_with_its_label() -> None:
    chosen = reference.choose(DUAL_AUDIO, _options(pgs_ref_stream="0:s:1"), "en")
    assert chosen == ReferenceTrack(ReferenceKind.PGS, 1, "spa", "Signs & Songs")


def test_an_unmappable_specifier_is_left_to_ffsubsync() -> None:
    assert reference.choose(DUAL_AUDIO, _options(pgs_ref_stream="0:7"), "en") is None


def test_only_ffsubsync_reads_pgs() -> None:
    track = ReferenceTrack(ReferenceKind.PGS, 2, "eng")
    assert reference.effective(track, EngineId.FFSUBSYNC) == track
    assert reference.effective(track, EngineId.ALASS).kind is ReferenceKind.AUDIO
    assert not reference.usable(DUAL_AUDIO[2], EngineId.ALASS)
    assert reference.usable(Track(index=0, codec="subrip"), EngineId.ALASS)


def test_a_track_survives_serialization() -> None:
    track = ReferenceTrack(ReferenceKind.PGS, 2, "eng", "Full", chosen=True)
    assert ReferenceTrack.from_dict(track.to_dict()) == track
    assert ReferenceTrack.from_dict({"kind": "pgs"}) is None
    assert ReferenceTrack.from_dict({"kind": "bogus", "index": 1}) is None
    assert ReferenceTrack.from_dict(None) is None


def test_labels_read_well_in_a_table() -> None:
    assert ReferenceTrack.audio().describe() == "Audio"
    assert ReferenceTrack(ReferenceKind.PGS, 2, "eng").describe() == "PGS · eng"
    assert ReferenceTrack(ReferenceKind.TEXT, 5).describe() == "Text · s:5"


# -- the queue -----------------------------------------------------------


class _Process:
    instances: ClassVar[list[_Process]] = []

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        _Process.instances.append(self)

    def cancel(self) -> None:
        pass

    def run(self, **_: Any) -> dict[str, SyncResult]:
        if self.spec["serialize_speech"]:
            np.savez(self.spec["speech_output"], speech=[0, 1, 0])
        return {
            item["id"]: SyncResult(status=JobStatus.SUCCEEDED)
            for item in self.spec["items"]
        }


@pytest.fixture(autouse=True)
def _stub(monkeypatch: pytest.MonkeyPatch) -> None:
    _Process.instances = []
    monkeypatch.setattr(queue_module, "SyncProcess", _Process)
    monkeypatch.setattr(queue_module, "validate", lambda job: None)
    monkeypatch.setattr(embedded, "probe_tracks", lambda *_: list(DUAL_AUDIO))


@pytest.fixture
def build_queue(
    store: JobStore, config: AppConfig, tmp_path: Path
) -> Callable[[], SyncQueue]:
    return lambda: SyncQueue(store, config, SpeechCache(tmp_path / "speech"))


def _job(tmp_path: Path, language: str) -> SyncJob:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"not really a remux")
    subtitle = tmp_path / f"Movie.{language}.srt"
    subtitle.touch()
    return SyncJob(
        reference_path=video,
        subtitle_path=subtitle,
        output_path=tmp_path / f"Movie.{language}.synced.srt",
        language=language,
        group_key="movie",
    )


def _wait_resolved(queue: SyncQueue) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(job.reference_track for job in queue.snapshot()):
            return
        time.sleep(0.01)
    raise AssertionError("tracks were never resolved")


def _drain(queue: SyncQueue) -> None:
    queue.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        counts = queue.counts()
        if counts.ready == 0 and counts.running == 0:
            return
        time.sleep(0.01)
    raise AssertionError("queue did not drain")


def test_adding_a_job_resolves_its_track(
    build_queue: Callable[[], SyncQueue], tmp_path: Path
) -> None:
    queue = build_queue()
    queue.add([_job(tmp_path, "en")])
    _wait_resolved(queue)
    assert queue.snapshot()[0].reference_track == ReferenceTrack(
        ReferenceKind.PGS, 2, "eng", ""
    )


def test_the_worker_is_told_the_track(
    build_queue: Callable[[], SyncQueue], tmp_path: Path
) -> None:
    queue = build_queue()
    queue.add([_job(tmp_path, "en")])
    _drain(queue)
    assert _Process.instances[0].spec["reference_track"]["index"] == 2


def test_languages_on_different_tracks_run_apart(
    build_queue: Callable[[], SyncQueue], tmp_path: Path
) -> None:
    queue = build_queue()
    queue.add([_job(tmp_path, "en"), _job(tmp_path, "es")])
    _drain(queue)
    indexes = sorted(p.spec["reference_track"]["index"] for p in _Process.instances)
    assert indexes == [2, 3]


def test_a_cached_array_is_never_reused_for_another_track(
    build_queue: Callable[[], SyncQueue], tmp_path: Path
) -> None:
    """The bug that made a retry fail the same way: one key for every track."""
    first = build_queue()
    first.add([_job(tmp_path, "en")])
    _drain(first)
    assert _Process.instances[-1].spec["serialize_speech"] is True

    second = build_queue()
    job = _job(tmp_path, "en")
    job.reference_track = ReferenceTrack(ReferenceKind.PGS, 0, chosen=True)
    second.add([job])
    _drain(second)
    assert _Process.instances[-1].spec["serialize_speech"] is True
    assert not _Process.instances[-1].spec["reference"].endswith(".npz")


def test_choosing_a_track_requeues_a_failed_job(
    build_queue: Callable[[], SyncQueue], tmp_path: Path
) -> None:
    queue = build_queue()
    failed, done = _job(tmp_path, "en"), _job(tmp_path, "es")
    failed.status, done.status = JobStatus.FAILED, JobStatus.SUCCEEDED
    queue.add([failed, done])
    pick = ReferenceTrack(ReferenceKind.PGS, 3, "spa", chosen=True)
    assert queue.set_reference([failed.id, done.id], pick) == 1
    assert failed.status is JobStatus.QUEUED
    assert failed.reference_track == pick
    assert done.reference_track != pick


def test_automatic_resolves_again(
    build_queue: Callable[[], SyncQueue], tmp_path: Path
) -> None:
    queue = build_queue()
    job = _job(tmp_path, "en")
    job.reference_track = ReferenceTrack.audio(chosen=True)
    queue.add([job])
    queue.set_reference([job.id], None)
    _wait_resolved(queue)
    assert job.reference_track is not None
    assert (job.reference_track.index, job.reference_track.chosen) == (2, False)


def test_a_chosen_track_survives_a_restart(
    store: JobStore, tmp_path: Path, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("A")
    job.reference_track = ReferenceTrack(ReferenceKind.PGS, 2, "eng", chosen=True)
    store.save_jobs([job])
    assert store.load_jobs()[0].reference_track == job.reference_track


# -- the worker ----------------------------------------------------------


def _spec(track: ReferenceTrack | None, stream: str = "auto") -> dict[str, Any]:
    return {
        "engine": "ffsubsync",
        "reference": "Movie.mkv",
        "options": {"ffsubsync": {"pgs_ref_stream": stream}},
        "reference_track": track.to_dict() if track else None,
        "items": [{"id": "job-1", "language": "en"}],
    }


def _channel() -> tuple[EventChannel, io.StringIO]:
    sink = io.StringIO()
    return EventChannel(sink), sink


def test_the_worker_uses_the_queues_track_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> list[Track]:
        raise AssertionError("the worker probed again")

    monkeypatch.setattr(embedded, "probe_tracks", fail)
    spec = _spec(ReferenceTrack(ReferenceKind.PGS, 2, "eng"))
    channel, sink = _channel()
    use_pgs_reference(spec, channel)
    assert spec["options"]["ffsubsync"]["pgs_ref_stream"] == "s:2"
    assert "s:2" in json.loads(sink.getvalue().splitlines()[-1])["message"]


def test_a_chosen_audio_reference_turns_pgs_off() -> None:
    spec = _spec(ReferenceTrack.audio(chosen=True))
    channel, _ = _channel()
    use_pgs_reference(spec, channel)
    assert spec["options"]["ffsubsync"]["pgs_ref_stream"] == ""


def test_a_message_names_the_stream_once() -> None:
    assert (
        ReferenceTrack(ReferenceKind.PGS, 2, "eng").with_stream() == "PGS · eng (s:2)"
    )
    assert ReferenceTrack(ReferenceKind.TEXT, 5).with_stream() == "Text · s:5"
    assert ReferenceTrack.audio().with_stream() == "Audio"


def test_a_failure_says_what_it_was_aligned_against() -> None:
    from subsyncr.core.engines.ffsubsync import aligned_against

    pgs = _spec(ReferenceTrack(ReferenceKind.PGS, 0, "eng", "Signs & Songs"), "s:0")
    assert aligned_against(pgs) == "PGS · eng · Signs & Songs (s:0)"
    assert aligned_against(_spec(ReferenceTrack.audio())) == "the audio"
    # A text track that never extracted left the video as the reference.
    assert aligned_against(_spec(ReferenceTrack(ReferenceKind.TEXT, 4))) == "the audio"
    assert aligned_against(_spec(None)) == ""
