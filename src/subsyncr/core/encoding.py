"""Working out what character set a subtitle file is actually in.

Subtitles are one of the last places where non-UTF-8 text is still common:
files from the 2000s are routinely cp1251, cp1256 or Big5, and nothing in them
says so. Guessing wrong turns dialogue into mojibake, and alass will not even
start without being told.

``charset-normalizer`` does the detection. Everything here is about turning its
answer into something a specific tool will accept.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from charset_normalizer import from_path

# What "infer" means when detection has nothing useful to say.
FALLBACK = "utf-8"

# Only look at the head of the file. Enough to characterise the encoding,
# and it keeps a batch of hundreds of subtitles quick.
_SAMPLE_BYTES = 64 * 1024


@lru_cache(maxsize=512)
def _detect(path: str, size: int, modified: int) -> str:
    """Cached by identity, so a group of jobs sharing a file pays once.

    ``size`` and ``modified`` are in the key rather than ignored: they make an
    edited file a different question rather than a stale answer.
    """
    del size, modified
    try:
        sample = Path(path).read_bytes()[:_SAMPLE_BYTES]
    except OSError:
        return FALLBACK

    # UTF-8 is self-validating: a byte sequence that decodes cleanly as UTF-8
    # is essentially never anything else, because the multi-byte patterns it
    # requires almost never occur by chance in legacy text. Statistical
    # detection can still prefer a legacy codec here - a short Cyrillic line
    # reads convincingly as several of them - so check this first rather than
    # putting it to a vote.
    codec = "utf-8-sig" if sample.startswith(b"\xef\xbb\xbf") else FALLBACK
    if _decodes_as(sample, codec):
        return codec

    try:
        matches = from_path(path, steps=4, chunk_size=_SAMPLE_BYTES // 4)
    except OSError:
        return FALLBACK
    best = matches.best()
    if best is None or not best.encoding:
        return FALLBACK
    return normalize(best.encoding)


def _decodes_as(sample: bytes, codec: str) -> bool:
    try:
        sample.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return False
    return True


def detect(path: Path) -> str:
    """The character set this file appears to be in."""
    try:
        stat = path.stat()
    except OSError:
        return FALLBACK
    return _detect(str(path), stat.st_size, int(stat.st_mtime))


def normalize(name: str) -> str:
    """Canonical spelling for an encoding name.

    charset-normalizer answers with Python's own names, which are mostly what
    other tools want too. The exceptions are the ASCII-compatible answers: a
    file detected as pure ASCII is safely read as UTF-8, and saying UTF-8 keeps
    any non-ASCII a later edit introduces readable.
    """
    lowered = name.strip().lower().replace("_", "-")
    if lowered in {"ascii", "us-ascii"}:
        return FALLBACK
    # charset-normalizer reports UTF-8 with a BOM separately; the codec that
    # strips the BOM is the one a reader wants.
    if lowered in {"utf-8-sig", "utf8-sig"}:
        return "utf-8-sig"
    return lowered


def resolve(path: Path, configured: str) -> str:
    """The encoding to use for this file, honouring an explicit setting.

    ``infer`` (the default, and ffsubsync's own word for it) means detect.
    Anything else is the user overriding us, and is passed through untouched.
    """
    wanted = configured.strip().lower()
    if wanted in {"", "infer", "auto", "detect"}:
        return detect(path)
    return wanted


def read_text(path: Path, configured: str = "infer") -> str:
    """Read a subtitle as text, never raising on a bad byte."""
    return path.read_text(encoding=resolve(path, configured), errors="replace")
