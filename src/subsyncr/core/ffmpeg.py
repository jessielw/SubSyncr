"""Discovery of the ffmpeg/ffprobe pair ffsubsync shells out to."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


@dataclass(frozen=True, slots=True)
class FFmpegStatus:
    ready: bool
    message: str
    directory: Path | None = None
    ffmpeg: Path | None = None
    ffprobe: Path | None = None

    @property
    def has_ffprobe(self) -> bool:
        return self.ffprobe is not None


def tool_names() -> tuple[str, str]:
    suffix = ".exe" if os.name == "nt" else ""
    return f"ffmpeg{suffix}", f"ffprobe{suffix}"


def _from_directory(directory: Path) -> FFmpegStatus:
    ffmpeg_name, ffprobe_name = tool_names()
    ffmpeg = directory / ffmpeg_name
    ffprobe = directory / ffprobe_name
    if not ffmpeg.is_file():
        return FFmpegStatus(False, f"ffmpeg was not found in {directory}", directory)
    if not ffprobe.is_file():
        # ffsubsync still syncs without ffprobe, but it cannot determine the
        # reference duration, so progress goes indeterminate.
        return FFmpegStatus(
            True,
            f"ffmpeg ready, but ffprobe is missing from {directory} "
            "(progress will be indeterminate)",
            directory,
            ffmpeg,
            None,
        )
    return FFmpegStatus(True, f"ffmpeg ready: {directory}", directory, ffmpeg, ffprobe)


def probe(configured_path: str = "") -> FFmpegStatus:
    """Resolve ffmpeg/ffprobe from an explicit path, else from ``PATH``.

    ``configured_path`` may be either the directory holding the binaries or the
    ffmpeg binary itself, matching what ffsubsync accepts for ``--ffmpeg-path``.
    """
    configured = configured_path.strip()
    if configured:
        candidate = Path(configured).expanduser()
        directory = candidate.parent if candidate.is_file() else candidate
        if not directory.is_dir():
            return FFmpegStatus(
                False, f"Configured ffmpeg path does not exist: {candidate}"
            )
        return _from_directory(directory)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return FFmpegStatus(False, "ffmpeg was not found on PATH")
    ffprobe = shutil.which("ffprobe")
    directory = Path(ffmpeg).parent
    if ffprobe is None:
        return FFmpegStatus(
            True,
            f"ffmpeg ready: {directory} (ffprobe missing, progress will be "
            "indeterminate)",
            directory,
            Path(ffmpeg),
            None,
        )
    return FFmpegStatus(
        True, f"ffmpeg ready: {directory}", directory, Path(ffmpeg), Path(ffprobe)
    )
