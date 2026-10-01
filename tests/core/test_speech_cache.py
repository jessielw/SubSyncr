"""Speech array caching and its ffsubsync-imposed constraints."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from support import sync_options

from subsyncr.core.models import FFSubSyncOptions
from subsyncr.core.speech_cache import SpeechCache


def _reference(tmp_path: Path, name: str = "ep.mkv") -> Path:
    path = tmp_path / name
    path.write_bytes(b"video bytes")
    return path


def test_miss_then_hit(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    options = FFSubSyncOptions()
    assert cache.lookup(reference, options) is None

    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    harvested = cache.harvest(reference, options, staged)
    assert harvested is not None
    assert cache.lookup(reference, options) == harvested


def test_harvest_leaves_nothing_beside_the_media(tmp_path: Path) -> None:
    """ffsubsync writes the array next to the video; we must not leave it."""
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    cache.harvest(reference, FFSubSyncOptions(), staged)
    assert not staged.exists()


def test_harvest_without_a_sidecar_is_not_an_error(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache")
    assert (
        cache.harvest(_reference(tmp_path), FFSubSyncOptions(), cache.stage()) is None
    )


def test_extraction_settings_key_the_cache(tmp_path: Path) -> None:
    """An array built with one VAD must never be reused under another."""
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    cache.harvest(reference, sync_options(vad="webrtc").ffsubsync, staged)
    assert cache.lookup(reference, sync_options(vad="webrtc").ffsubsync) is not None
    assert cache.lookup(reference, sync_options(vad="auditok").ffsubsync) is None
    assert cache.lookup(reference, sync_options(start_seconds=30).ffsubsync) is None


def test_source_stream_keys_the_cache(tmp_path: Path) -> None:
    """PGS timings and audio speech from one video are different arrays."""
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    cache.harvest(reference, sync_options(pgs_ref_stream="").ffsubsync, staged)
    pgs = sync_options(pgs_ref_stream="auto").ffsubsync
    second_audio = sync_options(pgs_ref_stream="", reference_stream="a:1").ffsubsync
    assert cache.lookup(reference, pgs) is None
    assert cache.lookup(reference, second_audio) is None


def test_unrelated_settings_do_not_key_the_cache(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    cache.harvest(reference, FFSubSyncOptions(), staged)
    assert cache.lookup(reference, sync_options(encoding="utf-8").ffsubsync) is not None


def test_a_changed_video_invalidates_the_cache(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    options = FFSubSyncOptions()
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    cache.harvest(reference, options, staged)
    assert cache.lookup(reference, options) is not None

    reference.write_bytes(b"a completely different video file")
    assert cache.lookup(reference, options) is None


def test_subtitle_references_are_never_cached(tmp_path: Path) -> None:
    """They need no audio extraction, so there is nothing to save."""
    cache = SpeechCache(tmp_path / "cache")
    subtitle = tmp_path / "reference.srt"
    subtitle.write_text("1\n", encoding="utf-8")
    assert cache.lookup(subtitle, FFSubSyncOptions()) is None


def test_disabled_cache_never_hits(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache", enabled=False)
    reference = _reference(tmp_path)
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    cache.harvest(reference, FFSubSyncOptions(), staged)
    assert cache.lookup(reference, FFSubSyncOptions()) is None
    assert cache.claim_producer("key") is False


def test_only_one_producer_per_group(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache")
    assert cache.claim_producer("group") is True
    assert cache.claim_producer("group") is False
    # A different video is unaffected.
    assert cache.claim_producer("other") is True
    cache.release_producer("group")
    assert cache.claim_producer("group") is True


def test_release_without_a_claim_is_safe(tmp_path: Path) -> None:
    SpeechCache(tmp_path / "cache").release_producer("never claimed")


def test_size_and_clear(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache")
    assert cache.size_bytes() == 0
    for name in ("a.mkv", "b.mkv"):
        reference = _reference(tmp_path, name)
        staged = cache.stage()
        np.savez(staged, speech=[0, 1, 0])
        cache.harvest(reference, FFSubSyncOptions(), staged)
    assert cache.size_bytes() > 0
    assert cache.clear() == 2
    assert cache.size_bytes() == 0


def test_harvest_refuses_files_it_did_not_create(tmp_path: Path):
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    unrelated = reference.with_suffix(".npz")
    unrelated.write_bytes(b"user data")
    with pytest.raises(ValueError, match="not owned"):
        cache.harvest(reference, FFSubSyncOptions(), unrelated)
    assert unrelated.read_bytes() == b"user data"


def test_parallel_extractions_have_distinct_staging_files(tmp_path: Path):
    cache = SpeechCache(tmp_path / "cache")
    first, second = cache.stage(), cache.stage()
    assert first != second
    assert first.parent == second.parent == cache.directory.resolve()


def test_interrupted_extraction_does_not_poison_cache(tmp_path: Path):
    cache = SpeechCache(tmp_path / "cache")
    reference = _reference(tmp_path)
    staged = cache.stage()
    staged.write_bytes(b"PK\x03\x04interrupted zip")
    assert cache.harvest(reference, FFSubSyncOptions(), staged) is None
    assert cache.lookup(reference, FFSubSyncOptions()) is None
    assert not staged.exists()


def test_clear_does_not_remove_active_extraction(tmp_path: Path):
    cache = SpeechCache(tmp_path / "cache")
    staged = cache.stage()
    np.savez(staged, speech=[0, 1, 0])
    assert cache.clear() == 0
    assert staged.is_file()
