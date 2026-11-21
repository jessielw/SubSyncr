"""Deciding, before a job runs, exactly what it aligns against.

The worker used to settle this itself: "auto" became a PGS track, or an
extracted text track, only once the group was already running. That kept the
choice out of sight, out of the user's reach, and out of the speech cache's
key - a cached array built from the wrong track was filed under "auto" and
handed back on every retry.

Here the same rules run when a job is queued. The answer is stored on the job,
shown in the queue, and can be overridden; the worker then does what it says.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import threading

from subsyncr.core import embedded
from subsyncr.core.embedded import PGS_CODEC, Track
from subsyncr.core.engines import EngineId
from subsyncr.core.language import canonical
from subsyncr.core.models import ReferenceKind, ReferenceTrack, SyncOptions

# The forms the settings box documents ("s:2"), plus ffsubsync's own "0:s:2".
_EXPLICIT_PGS = re.compile(r"^(?:0:)?s:(\d+)$")


def from_track(track: Track, *, chosen: bool = False) -> ReferenceTrack:
    return ReferenceTrack(
        kind=ReferenceKind.TEXT if track.is_text else ReferenceKind.PGS,
        index=track.index,
        language=track.language,
        title=track.title,
        chosen=chosen,
    )


def choose(
    tracks: list[Track], options: SyncOptions, language: str | None
) -> ReferenceTrack | None:
    """Apply the settings' rules to one video's tracks.

    Same order the worker always used: an extracted text track if that is
    enabled and one exists, then the PGS setting, then the audio. None means
    the PGS setting is an ffmpeg specifier we cannot map to a track, so the
    worker passes it to ffsubsync untouched.
    """
    if options.extract_embedded:
        text = embedded.choose(tracks, language)
        if text is not None:
            return from_track(text)
    setting = options.ffsubsync.pgs_ref_stream.strip()
    if not setting:
        return ReferenceTrack.audio()
    if setting == "auto":
        pgs = embedded.choose_pgs(tracks, language)
        return from_track(pgs) if pgs is not None else ReferenceTrack.audio()
    match = _EXPLICIT_PGS.match(setting)
    if match is None:
        return None
    index = int(match[1])
    known = next((track for track in tracks if track.index == index), None)
    return ReferenceTrack(
        kind=ReferenceKind.PGS,
        index=index,
        language=known.language if known else "",
        title=known.title if known else "",
    )


@dataclass(frozen=True)
class LanguageChoice:
    """One kind of track in one language, e.g. the English PGS, in any video.

    Stream numbers differ from episode to episode; "the full English PGS
    track" is what a season's worth of selected jobs can share.
    """

    kind: ReferenceKind
    key: str  # What tracks are matched on: "en" for both "eng" and "en".
    shown: str  # The tag as the first video spells it, e.g. "eng".

    @property
    def label(self) -> str:
        return ReferenceTrack(self.kind, None, self.shown).describe()

    def in_video(self, tracks: Sequence[Track]) -> ReferenceTrack | None:
        """This video's best full track of this kind and language."""
        matching = [
            track
            for track in tracks
            if _offerable(track)
            and not track.is_partial
            and _kind(track) is self.kind
            and _language_key(track) == self.key
        ]
        best = (
            embedded.choose_pgs(matching)
            if self.kind is ReferenceKind.PGS
            else embedded.choose(matching)
        )
        return from_track(best, chosen=True) if best is not None else None


def shared_languages(videos: Sequence[Sequence[Track]]) -> list[LanguageChoice]:
    """Each (kind, language) that every video has a full track for.

    Ordered as the first video lists them. A language some episode only has
    as Signs & Songs is left out: picking it there would bring the failure
    back.
    """
    if not videos:
        return []

    def keys(tracks: Sequence[Track]) -> set[tuple[ReferenceKind, str]]:
        return {
            (_kind(track), key)
            for track in tracks
            if _offerable(track)
            and not track.is_partial
            and (key := _language_key(track))
        }

    common = set.intersection(*(keys(tracks) for tracks in videos))
    choices: list[LanguageChoice] = []
    for track in videos[0]:
        key = _language_key(track)
        if key is None or (_kind(track), key) not in common:
            continue
        common.discard((_kind(track), key))
        choices.append(LanguageChoice(_kind(track), key, track.language))
    return choices


def _offerable(track: Track) -> bool:
    """Text, or the one image format an engine reads (PGS)."""
    return track.is_text or track.codec == PGS_CODEC


def _kind(track: Track) -> ReferenceKind:
    return ReferenceKind.TEXT if track.is_text else ReferenceKind.PGS


def _language_key(track: Track) -> str | None:
    tag = track.language.strip()
    if not tag or tag.casefold() == "und":
        return None
    return canonical(tag) or tag.casefold()


def usable(track: Track, engine: EngineId) -> bool:
    """Whether this engine can align against this track at all.

    Only ffsubsync reads PGS timings. lapse searches embedded tracks itself, so
    nothing chosen here reaches it.
    """
    if engine is EngineId.LAPSE:
        return False
    return track.is_text or engine is EngineId.FFSUBSYNC


def effective(choice: ReferenceTrack, engine: EngineId) -> ReferenceTrack:
    """What an engine will actually use, given the job's choice."""
    if choice.kind is ReferenceKind.AUDIO or engine is EngineId.LAPSE:
        return ReferenceTrack.audio(chosen=choice.chosen)
    if choice.kind is ReferenceKind.PGS and engine is not EngineId.FFSUBSYNC:
        return ReferenceTrack.audio(chosen=choice.chosen)
    return choice


class TrackProbe:
    """Each video's subtitle tracks, probed once per file version.

    ffprobe only reads the container header, but on a network share that is
    still a second or so - worth paying once per video, not once per subtitle.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracks: dict[tuple[str, int, int], list[Track]] = {}
        # The last answer per path, for callers that must not touch the disk.
        self._latest: dict[str, list[Track]] = {}

    def tracks(self, video: Path, ffmpeg_path: str = "") -> list[Track] | None:
        """The video's subtitle tracks, or None if it could not be read."""
        key = _identity(video)
        if key is None:
            return None  # Missing, or a share that is not mounted.
        with self._lock:
            cached = self._tracks.get(key)
            if cached is not None:
                self._latest[str(video)] = cached
        if cached is not None:
            return cached
        found = embedded.read_tracks(video, ffmpeg_path)
        if found is not None:
            # A failure may be a share that blinked; ask again next time.
            with self._lock:
                self._tracks[key] = found
                self._latest[str(video)] = found
        return found

    def peek(self, video: Path) -> list[Track] | None:
        """The tracks last read for this path, without going near the file.

        For the GUI thread, where even a ``stat`` on a share that is still
        waking up can stall the window. None means not read yet (or unreadable).
        """
        with self._lock:
            return self._latest.get(str(video))

    def cached(self, video: Path) -> list[Track] | None:
        key = _identity(video)
        if key is None:
            return None
        with self._lock:
            return self._tracks.get(key)


def _identity(video: Path) -> tuple[str, int, int] | None:
    try:
        stat = video.stat()
        return (str(video.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None
