"""One GUI queue owner per data directory, with dead-process recovery."""

from pathlib import Path

from PySide6.QtCore import QLockFile


def acquire_gui_lock(directory: Path) -> QLockFile:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / "gui.lock"))
    # A long sync must not make a live process's lock expire. Qt still detects
    # dead owner PIDs, so a crash does not require manually deleting this file.
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if lock.error() == QLockFile.LockError.LockFailedError:
            raise RuntimeError(
                "SubSyncr is already open for this data folder. "
                "Use the existing window, or close it before opening another."
            )
        raise RuntimeError(f"Could not lock the SubSyncr data folder: {directory}")
    return lock
