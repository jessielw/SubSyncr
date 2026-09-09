"""The engine registry: one ``EngineSpec`` per aligner we can drive.

Importing this module is cheap and side-effect free. It deliberately does *not*
import the engine implementations - those pull in ffsubsync, numpy and friends,
and only the worker process ever needs them.

Registered here means "the app knows this engine exists and what it can do".
Whether the machine can actually *run* it is a separate question, answered by
``core.binaries`` for the ones that are native executables.
"""

from __future__ import annotations

from subsyncr.core.engines.base import (
    EngineId,
    EngineKind,
    EngineSpec,
    Reported,
)

__all__ = [
    "EngineId",
    "EngineKind",
    "EngineSpec",
    "Reported",
    "all_specs",
    "get",
    "registered",
]

FFSUBSYNC = EngineSpec(
    id=EngineId.FFSUBSYNC,
    label="ffsubsync",
    kind=EngineKind.PYTHON,
    module="subsyncr.core.engines.ffsubsync",
    homepage="https://github.com/smacke/ffsubsync",
    # The whole group design exists because this is True here and nowhere else.
    shares_reference=True,
    subtitle_reference=True,
    input_suffixes=frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub"}),
    # ffsubsync parses .sub but raises NotImplementedError writing it.
    output_suffixes=frozenset({".srt", ".ass", ".ssa", ".vtt"}),
    needs_ffmpeg=True,
    reports=frozenset({Reported.OFFSET, Reported.SCALE, Reported.SCORE}),
)


ALASS = EngineSpec(
    id=EngineId.ALASS,
    label="alass",
    kind=EngineKind.BINARY,
    module="subsyncr.core.engines.alass",
    homepage="https://github.com/kaegi/alass",
    # A separate process per subtitle; there is nothing to share between them.
    shares_reference=False,
    subtitle_reference=True,
    input_suffixes=frozenset({".srt", ".ass", ".ssa", ".sub", ".idx"}),
    output_suffixes=frozenset({".srt", ".ass", ".ssa", ".sub", ".idx"}),
    needs_ffmpeg=True,
    # It prints the shift it applied and the framerate ratio it guessed, but
    # nothing resembling a confidence.
    reports=frozenset({Reported.OFFSET, Reported.SCALE}),
)

LAPSE = EngineSpec(
    id=EngineId.LAPSE,
    label="lapse",
    kind=EngineKind.BINARY,
    module="subsyncr.core.engines.lapse",
    homepage="https://github.com/Schwponaco-org/lapse",
    shares_reference=False,
    subtitle_reference=True,
    input_suffixes=frozenset({".srt", ".ass", ".ssa", ".vtt"}),
    output_suffixes=frozenset({".srt", ".ass", ".ssa", ".vtt"}),
    needs_ffmpeg=True,
    # The only engine of the four that returns a structured result.
    reports=frozenset({Reported.OFFSET, Reported.SCALE, Reported.SCORE}),
)


AUTOSUBSYNC = EngineSpec(
    id=EngineId.AUTOSUBSYNC,
    label="autosubsync",
    kind=EngineKind.PYTHON,
    module="subsyncr.core.engines.autosubsync",
    homepage="https://github.com/oseiskar/autosubsync",
    shares_reference=False,
    # It classifies audio features, so there is nothing for it to do with a
    # subtitle reference. This is the capability the fallback chain checks
    # before wasting a process on it.
    subtitle_reference=False,
    input_suffixes=frozenset({".srt"}),
    output_suffixes=frozenset({".srt"}),
    needs_ffmpeg=True,
    reports=frozenset({Reported.OFFSET, Reported.SCALE, Reported.SCORE}),
)


_SPECS: dict[EngineId, EngineSpec] = {
    spec.id: spec
    for spec in (
        FFSUBSYNC,
        ALASS,
        LAPSE,
        AUTOSUBSYNC,
    )
}


def get(engine: EngineId | str) -> EngineSpec:
    """Look up a spec. Raises ``KeyError`` for an engine we do not implement.

    Strict on purpose: ``EngineId.parse`` exists to sanitise stored values at
    the boundary, and reusing it here would silently answer questions about
    "alass" with ffsubsync's capabilities.
    """
    if isinstance(engine, EngineId):
        return _SPECS[engine]
    try:
        key = EngineId(str(engine))
    except ValueError as exc:
        raise KeyError(engine) from exc
    return _SPECS[key]


def registered(engine: EngineId | str) -> bool:
    """Whether this engine is implemented in this build."""
    try:
        get(engine)
    except KeyError:
        return False
    return True


def all_specs() -> tuple[EngineSpec, ...]:
    """Every implemented engine, in registration order."""
    return tuple(_SPECS.values())
