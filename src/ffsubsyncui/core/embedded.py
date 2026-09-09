"""Using a video's own subtitle track as the sync reference.

Aligning against audio means decoding the whole film and deciding, frame by
frame, where speech is. Aligning against a subtitle track already inside the
container means comparing two lists of timestamps. The second is roughly a
thousand times cheaper and, when the embedded track is well timed, more
accurate - the timings are the release's own, not an estimate of them.

Most releases carry at least one text subtitle track. When one is there and the
user has asked for this, we pull it out with ffmpeg and hand the engine that
instead of the video. Failure is never fatal: anything that goes wrong falls
back to the original reference and the sync proceeds on audio.

Image-based tracks (PGS, VobSub) are skipped. There is no text in them to
compare - ffsubsync has a separate ``--pgs-ref-stream`` path for those.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile

from ffsubsyncui.core.ffmpeg import probe
from ffsubsyncui.core.language import canonical

# Codecs that are pictures rather than text. Extracting one produces nothing a
# timestamp comparison can use.
IMAGE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"})

# What we ask ffmpeg to write. Every engine reads SubRip.
_EXTRACT_SUFFIX = ".srt"

_TIMEOUT = 120.0


class ExtractionError(RuntimeError):
    """No usable embedded subtitle could be extracted."""


@dataclass(frozen=True, slots=True)
class Track:
    """One subtitle stream inside a container."""

    # Position among the *subtitle* streams, which is what ffmpeg's 0:s:N wants.
    index: int
    codec: str
    language: str = ""
    title: str = ""
    forced: bool = False
    default: bool = False
    frames: int = 0

    @property
    def is_text(self) -> bool:
        return self.codec not in IMAGE_CODECS


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def probe_tracks(video: Path, ffmpeg_path: str = "") -> list[Track]:
    """Every subtitle stream ffprobe can see in this file."""
    status = probe(ffmpeg_path)
    if not status.ready or status.ffprobe is None:
        return []
    command = [
        str(status.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title,NUMBER_OF_FRAMES"
        ":stream_disposition=forced,default",
        "-of",
        "json",
        str(video),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return []

    tracks: list[Track] = []
    for position, stream in enumerate(payload.get("streams") or []):
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        frames = tags.get("NUMBER_OF_FRAMES") or tags.get("NUMBER_OF_FRAMES-eng") or 0
        try:
            frame_count = int(frames)
        except (TypeError, ValueError):
            frame_count = 0
        tracks.append(
            Track(
                index=position,
                codec=str(stream.get("codec_name", "")),
                language=str(tags.get("language", "")).lower(),
                title=str(tags.get("title", "")),
                forced=bool(disposition.get("forced")),
                default=bool(disposition.get("default")),
                frames=frame_count,
            )
        )
    return tracks


def choose(tracks: list[Track], prefer_language: str | None = None) -> Track | None:
    """Pick the track most likely to be a faithful transcript of the dialogue.

    In order of importance: it has to be text; a track in the same language as
    the subtitle being synced is likeliest to share its structure; forced tracks
    are only the foreign-dialogue lines and cover a fraction of the runtime, so
    they are actively bad references; and between equals, the one with the most
    events describes more of the film.

    AutoSubSync picks by comparing timestamp counts, which needs every candidate
    extracted first. The container already knows all of this.
    """
    usable = [track for track in tracks if track.is_text]
    if not usable:
        return None
    wanted = canonical(prefer_language or "")

    def rank(track: Track) -> tuple[int, int, int, int]:
        language_match = (
            1 if wanted is not None and canonical(track.language) == wanted else 0
        )
        return (
            language_match,
            0 if track.forced else 1,
            1 if track.default else 0,
            track.frames,
        )

    return max(usable, key=rank)


def extract(
    video: Path,
    track: Track,
    destination: Path,
    ffmpeg_path: str = "",
) -> Path:
    """Write one subtitle stream out as SubRip. Returns the file written."""
    status = probe(ffmpeg_path)
    if not status.ready or status.ffmpeg is None:
        raise ExtractionError("ffmpeg is needed to extract embedded subtitles")
    command = [
        str(status.ffmpeg),
        "-y",
        "-v",
        "error",
        # Never let ffmpeg sit waiting on a console it does not have.
        "-nostdin",
        "-i",
        str(video),
        "-map",
        f"0:s:{track.index}",
        "-c:s",
        "srt",
        str(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractionError(f"could not run ffmpeg: {exc}") from exc
    if completed.returncode != 0 or not destination.is_file():
        detail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
        raise ExtractionError(f"ffmpeg could not extract the track: {detail[0]}")
    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise ExtractionError("the extracted subtitle was empty")
    return destination


def reference_from(
    video: Path,
    workdir: Path,
    *,
    prefer_language: str | None = None,
    ffmpeg_path: str = "",
) -> tuple[Path, Track] | None:
    """Extract the best embedded subtitle to use in place of the video.

    Returns None when there is nothing usable, which is a normal answer and not
    an error - the caller carries on with the video.
    """
    chosen = choose(probe_tracks(video, ffmpeg_path), prefer_language)
    if chosen is None:
        return None
    handle, name = tempfile.mkstemp(
        dir=str(workdir), prefix=".embedded-", suffix=_EXTRACT_SUFFIX
    )
    os.close(handle)
    destination = Path(name)
    try:
        extract(video, chosen, destination, ffmpeg_path)
    except ExtractionError:
        destination.unlink(missing_ok=True)
        raise
    return destination, chosen
