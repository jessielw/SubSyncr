"""Converting subtitles between formats, so an engine's limits are not the user's.

Each engine reads a different set of formats. autosubsync takes SubRip and
nothing else; alass will not look at WebVTT; ffsubsync parses MicroDVD but
cannot write it. Left alone that would mean telling someone their perfectly
ordinary ``.vtt`` cannot be synced, which is a poor answer when converting it
takes milliseconds.

So: anything an engine cannot read is converted to SubRip on the way in, and the
result is converted back to whatever was asked for on the way out. Conversions
happen in a temporary file beside the target and are cleaned up afterwards - the
user's own files are never rewritten as a side effect of choosing an engine.

``.idx``/``.sub`` VobSub pairs are the exception: they are bitmaps, not text, and
only alass can use them. They are passed through untouched rather than mangled.
"""

from __future__ import annotations

from collections.abc import Iterable
import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile

import pysubs2
from pysubs2.exceptions import Pysubs2Error

from subsyncr.core import encoding as encodings

# What we convert to when an engine cannot read the original. Every engine
# reads SubRip, and it is the format that loses the least in translation.
NEUTRAL_SUFFIX = ".srt"

# Image-based subtitles. There is no text to convert, so any attempt to would
# destroy them.
IMAGE_SUFFIXES = frozenset({".idx", ".sup"})

# What pysubs2 can actually parse and write, by extension.
CONVERTIBLE_SUFFIXES = frozenset(
    {".ass", ".ssa", ".srt", ".vtt", ".sub", ".smi", ".sami", ".ttml", ".json"}
)

# MicroDVD stores frame numbers rather than times, so reading or writing one
# needs a frame rate. pysubs2 raises without it.
_FRAME_BASED = frozenset({".sub"})
_DEFAULT_FPS = 23.976


class ConversionError(RuntimeError):
    """A subtitle could not be converted into a format the engine accepts."""


@dataclass(slots=True)
class Prepared:
    """Paths an engine should actually be given, and what to tidy up after."""

    reference: str
    subtitle: Path
    # Where the engine writes. May differ in format from what the user wants.
    output: Path
    # Where the finished subtitle has to end up, in the user's chosen format.
    final_output: Path
    temporary: list[Path] = field(default_factory=list)

    @property
    def needs_conversion_back(self) -> bool:
        return self.output != self.final_output

    def clean(self) -> None:
        for path in self.temporary:
            # A leftover temp file is not worth failing a finished sync over.
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)


def convertible(path: Path) -> bool:
    return path.suffix.lower() in CONVERTIBLE_SUFFIXES


def convert(
    source: Path,
    target: Path,
    *,
    encoding: str = "infer",
    output_encoding: str = "utf-8",
    fps: float = _DEFAULT_FPS,
) -> Path:
    """Rewrite ``source`` into ``target``'s format. Returns ``target``."""
    if IMAGE_SUFFIXES & {source.suffix.lower(), target.suffix.lower()}:
        raise ConversionError(
            f"{source.name} is an image-based subtitle and cannot be converted"
        )
    if not convertible(source):
        raise ConversionError(f"unsupported subtitle format: {source.suffix}")
    try:
        subtitles = pysubs2.load(
            str(source),
            encoding=encodings.resolve(source, encoding),
            fps=fps if source.suffix.lower() in _FRAME_BASED else None,
        )
    except (OSError, ValueError, UnicodeDecodeError, Pysubs2Error) as exc:
        raise ConversionError(f"could not read {source.name}: {exc}") from exc
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        subtitles.save(
            str(target),
            encoding=_writable_encoding(output_encoding, source, encoding),
            fps=fps if target.suffix.lower() in _FRAME_BASED else None,
        )
    except (OSError, ValueError, Pysubs2Error) as exc:
        raise ConversionError(f"could not write {target.name}: {exc}") from exc
    return target


def _writable_encoding(output_encoding: str, source: Path, encoding: str) -> str:
    """ "same" means keep whatever the input was; anything else is literal."""
    if output_encoding.strip().lower() in {"same", "source", "input"}:
        return encodings.resolve(source, encoding)
    return output_encoding.strip() or encodings.FALLBACK


def _temporary(directory: Path, stem: str, suffix: str) -> Path:
    handle, name = tempfile.mkstemp(
        dir=str(directory), prefix=f".{stem}-", suffix=suffix
    )
    # We only wanted the unique name; pysubs2 opens it itself.
    os.close(handle)
    return Path(name)


def prepare(
    reference: str,
    subtitle: Path,
    output: Path,
    *,
    accepted: Iterable[str],
    writable: Iterable[str],
    workdir: Path,
    encoding: str = "infer",
    output_encoding: str = "utf-8",
    reference_is_subtitle: bool = False,
) -> Prepared:
    """Put the inputs into formats the engine can read, and pick where it writes.

    Nothing is copied when the engine already accepts what it was given, which
    is the common case: the work only happens for the pairing that would
    otherwise have to be refused.
    """
    readable = {suffix.lower() for suffix in accepted}
    writeable = {suffix.lower() for suffix in writable}
    plan = Prepared(
        reference=reference, subtitle=subtitle, output=output, final_output=output
    )

    if reference_is_subtitle:
        source = Path(reference)
        if source.suffix.lower() not in readable and convertible(source):
            converted = _temporary(workdir, "reference", NEUTRAL_SUFFIX)
            plan.temporary.append(converted)
            convert(
                source,
                converted,
                encoding=encoding,
                output_encoding=output_encoding,
            )
            plan.reference = str(converted)

    if subtitle.suffix.lower() not in readable:
        if not convertible(subtitle):
            raise ConversionError(
                f"{subtitle.name} cannot be converted for this engine"
            )
        converted = _temporary(workdir, subtitle.stem, NEUTRAL_SUFFIX)
        plan.temporary.append(converted)
        convert(subtitle, converted, encoding=encoding, output_encoding=output_encoding)
        plan.subtitle = converted

    if output.suffix.lower() not in writeable:
        # The engine cannot produce the format the user asked for, so let it
        # write SubRip and convert the result back afterwards.
        staged = _temporary(workdir, output.stem, NEUTRAL_SUFFIX)
        plan.temporary.append(staged)
        plan.output = staged

    return plan


def finish(plan: Prepared, *, output_encoding: str = "utf-8") -> None:
    """Convert the engine's output back into the format that was asked for."""
    if not plan.needs_conversion_back:
        return
    convert(
        plan.output,
        plan.final_output,
        encoding="utf-8",
        output_encoding=output_encoding,
    )
