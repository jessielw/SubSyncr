"""What every sync engine is, expressed as data rather than as branching.

An engine is an external aligner - ffsubsync, alass, lapse, autosubsync - that
takes a reference and a subtitle and writes a shifted subtitle. They differ in
ways that matter to scheduling and validation, so those differences live in an
``EngineSpec`` and every decision reads the spec instead of testing engine names.

Deliberately free of imports from the rest of ``core``: ``models`` imports
``EngineId`` from here, so anything this module pulled in would be a cycle.
Engine *implementations* live in sibling modules loaded lazily by the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EngineId(StrEnum):
    FFSUBSYNC = "ffsubsync"
    ALASS = "alass"
    LAPSE = "lapse"
    AUTOSUBSYNC = "autosubsync"

    @classmethod
    def parse(cls, value: object, default: EngineId | None = None) -> EngineId:
        """Coerce a stored or hand-edited value, never raising."""
        try:
            return cls(str(value))
        except ValueError:
            return default if default is not None else cls.FFSUBSYNC


class EngineKind(StrEnum):
    """How the engine is invoked from inside the worker process."""

    # Importable Python, called directly. Ships with us as a dependency.
    PYTHON = "python"
    # A native binary we resolve or download and then spawn.
    BINARY = "binary"


class Reported(StrEnum):
    """Which ``SyncResult`` fields an engine can actually fill in.

    Frontends use this to hide columns an engine will always leave blank, rather
    than showing an empty cell that looks like a failure.
    """

    OFFSET = "offset"
    SCALE = "scale"
    SCORE = "score"


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """Everything about an engine that the rest of the app has to reason about."""

    id: EngineId
    label: str
    kind: EngineKind
    # The dotted module implementing ``run_group(spec, channel) -> int``. Loaded
    # lazily, inside the worker only, so importing the registry stays cheap and
    # never drags an engine's dependencies into the GUI process.
    module: str
    homepage: str
    # Can one reference extraction be shared across every subtitle in a group?
    # True only for ffsubsync, and it is the reason groups exist at all.
    shares_reference: bool
    # Accepts a subtitle file as the reference, rather than requiring audio.
    subtitle_reference: bool
    # Subtitle formats the engine will read. Inputs outside this set have to be
    # converted first (see core.subtitles).
    input_suffixes: frozenset[str]
    # Formats it can write. Usually the same set, but not for autosubsync.
    output_suffixes: frozenset[str]
    # Needs ffmpeg on hand when the reference is a video.
    needs_ffmpeg: bool
    reports: frozenset[Reported]

    def accepts_reference(self, suffix: str, *, is_subtitle: bool) -> bool:
        """Whether this engine can use the given reference at all."""
        if not is_subtitle:
            return True  # Any video is fair game; ffmpeg does the reading.
        return self.subtitle_reference and suffix.lower() in self.input_suffixes

    def accepts_subtitle(self, suffix: str) -> bool:
        return suffix.lower() in self.input_suffixes

    def can_write(self, suffix: str) -> bool:
        return suffix.lower() in self.output_suffixes
