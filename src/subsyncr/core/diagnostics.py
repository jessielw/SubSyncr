"""Copyable support details that exclude filenames, usernames and job history."""

from importlib.metadata import PackageNotFoundError, version
import platform
import sys

from subsyncr import __version__


def support_summary() -> str:
    lines = [
        f"SubSyncr: {__version__}",
        f"Platform: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {platform.python_version()}",
        f"Packaged build: {bool(getattr(sys, 'frozen', False))}",
    ]
    for package in ("pyside6-essentials", "ffsubsync", "autosubsync", "pysubs2"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "version metadata unavailable"
        lines.append(f"{package}: {installed}")
    return "\n".join(lines)
