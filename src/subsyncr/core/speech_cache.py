"""App-owned speech arrays, keyed by reference identity and extraction settings.

Workers receive a unique staging path inside this cache. They never create a
sidecar in a media folder, and harvest accepts only paths this instance issued.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import zipfile

from subsyncr.core.models import FFSubSyncOptions
from subsyncr.core.scanner import needs_ffmpeg


def _fingerprint(reference: Path, options: FFSubSyncOptions, source: str = "") -> str:
    """Stable id for "this file, extracted these settings, from this stream".

    ``source`` names the track the array came from ("pgs:2", "audio"). Empty
    keeps the original fingerprint, so arrays cached before it existed are
    still found for jobs that never resolved a track.
    """
    try:
        stat = reference.stat()
        identity = f"{reference.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(reference)
    parts = "|".join(str(part) for part in options.speech_cache_key())
    if source:
        parts = f"{parts}|source={source}"
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
        self._staged: set[Path] = set()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._producing.setdefault(key, threading.Lock())

    def cached_path(
        self, reference: Path, options: FFSubSyncOptions, source: str = ""
    ) -> Path:
        return self.directory / f"{_fingerprint(reference, options, source)}.npz"

    def lookup(
        self, reference: Path, options: FFSubSyncOptions, source: str = ""
    ) -> Path | None:
        """An existing speech array for this reference, if we have one."""
        if not self.enabled or not needs_ffmpeg(reference):
            return None
        candidate = self.cached_path(reference, options, source)
        return candidate if candidate.is_file() else None

    def stage(self) -> Path:
        """Reserve a unique file on the cache filesystem for one worker."""
        self.directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(
            prefix=".speech-", suffix=".npz", dir=self.directory
        )
        os.close(handle)
        path = Path(name).resolve()
        with self._guard:
            self._staged.add(path)
        return path

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

    def harvest(
        self,
        reference: Path,
        options: FFSubSyncOptions,
        staged: Path,
        source: str = "",
    ) -> Path | None:
        """Publish only the staging file issued for this extraction."""
        with self._guard:
            if staged not in self._staged:
                raise ValueError("Speech staging file is not owned by this cache")
            self._staged.remove(staged)
        destination = self.cached_path(reference, options, source)
        try:
            if not staged.is_file() or staged.stat().st_size == 0:
                return None
            # Cancellation can interrupt NumPy halfway through a ZIP write.
            # Keep such files out of the persistent cache rather than poisoning
            # every later sync against this reference.
            with zipfile.ZipFile(staged) as archive:
                if (
                    "speech.npy" not in archive.namelist()
                    or archive.testzip() is not None
                ):
                    return None
            os.replace(staged, destination)
            return destination
        except (OSError, zipfile.BadZipFile, EOFError):
            return None  # A cache failure must not fail a completed sync.
        finally:
            with contextlib.suppress(OSError):
                staged.unlink(missing_ok=True)

    def size_bytes(self) -> int:
        if not self.directory.is_dir():
            return 0
        return sum(
            path.stat().st_size
            for path in self.directory.glob("[!.]*.npz")
            if path.is_file()
        )

    def clear(self) -> int:
        """Delete every cached array. Returns how many were removed."""
        if not self.directory.is_dir():
            return 0
        removed = 0
        for path in self.directory.glob("[!.]*.npz"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
        return removed
