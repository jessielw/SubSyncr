"""Filesystem discovery of reference media and subtitle files."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

MEDIA_EXTENSIONS = frozenset(
    {
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".ts",
        ".webm",
        ".wmv",
        # Audio-only references work too; ffsubsync just needs speech.
        ".aac",
        ".ac3",
        ".dts",
        ".eac3",
        ".flac",
        ".m4a",
        ".mka",
        ".mp3",
        ".opus",
        ".wav",
    }
)

# ffsubsync detects a subtitle reference by the last three characters of the
# path (ffsubsync._ref_format), so ".vtt" is NOT treated as a subtitle there.
SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".sub", ".vtt"})

# What ffsubsync will accept as a *reference* without going through ffmpeg.
SUBTITLE_REFERENCE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".sub"})

# Serialized speech arrays produced by --serialize-speech.
SPEECH_EXTENSIONS = frozenset({".npy", ".npz"})

# Outputs we produce ourselves. Re-scanning a folder must not offer them back
# as fresh input, or a second run would sync already-synced subtitles.
_GENERATED_MARKERS = (".synced",)


def is_generated_output(path: Path) -> bool:
    """True for files this application previously wrote."""
    stem = path.stem.casefold()
    return any(stem.endswith(marker) for marker in _GENERATED_MARKERS)


def iter_files(folder: Path, *, recursive: bool) -> Iterator[Path]:
    iterator = folder.rglob("*") if recursive else folder.glob("*")
    for path in iterator:
        if path.is_file():
            yield path


def scan_folder(
    folder: Path,
    extensions: Iterable[str],
    *,
    recursive: bool = True,
    skip_generated: bool = True,
) -> list[Path]:
    """Sorted list of files under ``folder`` matching ``extensions``."""
    wanted = {extension.casefold() for extension in extensions}
    found = [
        path
        for path in iter_files(folder, recursive=recursive)
        if path.suffix.casefold() in wanted
        and not (skip_generated and is_generated_output(path))
    ]
    return sorted(found, key=lambda path: str(path).casefold())


def scan_media(folder: Path, *, recursive: bool = True) -> list[Path]:
    return scan_folder(folder, MEDIA_EXTENSIONS, recursive=recursive)


def scan_subtitles(folder: Path, *, recursive: bool = True) -> list[Path]:
    return scan_folder(folder, SUBTITLE_EXTENSIONS, recursive=recursive)


def scan_references(
    folder: Path, *, subtitles_as_reference: bool = False, recursive: bool = True
) -> list[Path]:
    """Files usable as a sync reference.

    A subtitle reference needs no ffmpeg at all, which makes aligning one
    subtitle against a known-good one close to instant.
    """
    extensions = (
        SUBTITLE_REFERENCE_EXTENSIONS if subtitles_as_reference else MEDIA_EXTENSIONS
    )
    return scan_folder(folder, extensions, recursive=recursive)


def needs_ffmpeg(reference: Path) -> bool:
    """Whether syncing against this reference has to decode audio.

    Subtitle and serialized-speech references skip ffmpeg entirely.
    """
    suffix = reference.suffix.casefold()
    return (
        suffix not in SUBTITLE_REFERENCE_EXTENSIONS and suffix not in SPEECH_EXTENSIONS
    )
