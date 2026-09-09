"""Reuse of ffsubsync serialized speech arrays across a subtitle group.

Extracting speech from a video is by far the slowest part of a sync. When one
video owns several subtitles (``ep.en.srt``, ``ep.es.srt``, ...) only the first
job needs to pay for it: ``--serialize-speech`` writes a ``.npz`` speech array,
and passing that ``.npz`` as the *reference* skips ffmpeg entirely.

Two constraints come from ffsubsync itself:

* The serialized path is not configurable - it is always
  ``os.path.splitext(reference)[0] + ".npz"``, i.e. beside the media file. We
  move it into our own cache directory afterwards so we never leave debris in
  the user's folders.
* The array bakes in the VAD, start offset, frame rate, non-speech label and
  duration limit used to build it, so all of those key the cache.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path
import shutil
import threading

from subsyncr.core.models import FFSubSyncOptions
from subsyncr.core.scanner import needs_ffmpeg


def _fingerprint(reference: Path, options: FFSubSyncOptions) -> str:
    """Stable id for "this file, extracted these settings"."""
    try:
        stat = reference.stat()
        identity = f"{reference.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        identity = str(reference)
    parts = "|".join(str(part) for part in options.speech_cache_key())
    return hashlib.sha256(f"{identity}|{parts}".encode()).hexdigest()[:32]


class SpeechCache:
    """A directory of ``.npz`` speech arrays keyed by reference and settings."""

    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        # One producer per group at a time; contenders fall back to a normal run
        # rather than block a worker thread.
        self._producing: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._producing.setdefault(key, threading.Lock())

    def cached_path(self, reference: Path, options: FFSubSyncOptions) -> Path:
        return self.directory / f"{_fingerprint(reference, options)}.npz"

    def lookup(self, reference: Path, options: FFSubSyncOptions) -> Path | None:
        """An existing speech array for this reference, if we have one."""
        if not self.enabled or not needs_ffmpeg(reference):
            return None
        candidate = self.cached_path(reference, options)
        return candidate if candidate.is_file() else None

    @staticmethod
    def sidecar_for(reference: Path) -> Path:
        """Where ffsubsync will write when ``--serialize-speech`` is set."""
        return reference.with_suffix(".npz")

    def claim_producer(self, key: str) -> bool:
        """Try to become the job that builds the array for this group."""
        if not self.enabled:
            return False
        return self._lock_for(key).acquire(blocking=False)

    def release_producer(self, key: str) -> None:
        lock = self._lock_for(key)
        if lock.locked():
            with contextlib.suppress(RuntimeError):
                lock.release()

    def harvest(self, reference: Path, options: FFSubSyncOptions) -> Path | None:
        """Move the sidecar ffsubsync just wrote into the cache.

        Returns the cached path, or ``None`` if nothing was produced.
        """
        sidecar = self.sidecar_for(reference)
        if not sidecar.is_file():
            return None
        destination = self.cached_path(reference, options)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sidecar), str(destination))
        except OSError:
            # Leaving a stray .npz beside the media is worse than losing the
            # cache entry, so make sure it goes either way.
            with contextlib.suppress(OSError):
                sidecar.unlink()
            return None
        return destination

    def size_bytes(self) -> int:
        if not self.directory.is_dir():
            return 0
        return sum(
            path.stat().st_size
            for path in self.directory.glob("*.npz")
            if path.is_file()
        )

    def clear(self) -> int:
        """Delete every cached array. Returns how many were removed."""
        if not self.directory.is_dir():
            return 0
        removed = 0
        for path in self.directory.glob("*.npz"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
        return removed
