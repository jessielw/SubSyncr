"""Language and variant tags carried in subtitle filenames.

Release subtitles conventionally tag the language in the stem: ``ep.en.srt``,
``ep.eng.forced.srt``, ``ep.pt-BR.sdh.srt``. Those tags matter twice over:
they are the only thing distinguishing sibling subtitles for one video, and
they must be removed before a stem is compared against a video stem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

# Token -> ISO 639-1 code. Covers 639-1, the common 639-2/B codes, and English
# names, since all three appear in the wild.
_LANGUAGES: dict[str, str] = {}


def _register(code: str, *aliases: str) -> None:
    _LANGUAGES[code] = code
    for alias in aliases:
        _LANGUAGES[alias] = code


_register("en", "eng", "english")
_register("es", "spa", "spanish", "castellano", "latino", "espanol")
_register("fr", "fre", "fra", "french", "francais")
_register("de", "ger", "deu", "german", "deutsch")
_register("it", "ita", "italian", "italiano")
_register("pt", "por", "portuguese", "brazilian", "brasil")
_register("nl", "dut", "nld", "dutch", "nederlands")
_register("sv", "swe", "swedish", "svenska")
_register("no", "nor", "norwegian", "norsk", "nb", "nob")
_register("da", "dan", "danish", "dansk")
_register("fi", "fin", "finnish", "suomi")
_register("is", "ice", "isl", "icelandic")
_register("pl", "pol", "polish", "polski")
_register("cs", "cze", "ces", "czech")
_register("sk", "slo", "slk", "slovak")
_register("hu", "hun", "hungarian", "magyar")
_register("ro", "rum", "ron", "romanian")
_register("bg", "bul", "bulgarian")
_register("el", "gre", "ell", "greek")
_register("tr", "tur", "turkish", "turkce")
_register("ru", "rus", "russian")
_register("uk", "ukr", "ukrainian")
_register("ar", "ara", "arabic")
_register("he", "heb", "hebrew")
_register("fa", "per", "fas", "persian", "farsi")
_register("hi", "hin", "hindi")
_register("th", "tha", "thai")
_register("vi", "vie", "vietnamese")
_register("id", "ind", "indonesian")
_register("ms", "may", "msa", "malay")
_register("ja", "jpn", "japanese")
_register("ko", "kor", "korean")
_register("zh", "chi", "zho", "chinese", "mandarin", "cmn", "hans", "hant")
_register("yue", "cantonese")
_register("hr", "hrv", "croatian")
_register("sr", "srp", "serbian")
_register("sl", "slv", "slovenian")
_register("et", "est", "estonian")
_register("lv", "lav", "latvian")
_register("lt", "lit", "lithuanian")
_register("ca", "cat", "catalan")
_register("eu", "baq", "eus", "basque")
_register("gl", "glg", "galician")

_FORCED_TOKENS = frozenset({"forced", "force"})
_SDH_TOKENS = frozenset(
    {"sdh", "cc", "hearingimpaired", "hearing", "impaired", "closedcaptions"}
)
_REGIONS = frozenset(
    {"br", "mx", "us", "gb", "uk", "ca", "au", "es", "pt", "cn", "tw", "hk", "latam"}
)

# A trailing "word" preceded by a separator, optionally with a region suffix
# ("pt-BR"). Anchored to the end so we only ever peel tags off the tail.
_TRAILING_TOKEN = re.compile(
    r"(?i)[._\-\s]+(?P<token>[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)?)$"
)

# Never peel more than this many trailing tokens; guards against a stem made
# entirely of words that happen to look like tags.
_MAX_TAGS = 4


@dataclass(frozen=True, slots=True)
class SubtitleTags:
    """What a subtitle filename says about itself."""

    language: str | None
    forced: bool
    sdh: bool
    base_stem: str

    @property
    def label(self) -> str:
        """Short human-readable tag, e.g. ``en (forced)``."""
        if self.language is None:
            return "forced" if self.forced else ("sdh" if self.sdh else "")
        flags = [
            flag for flag, on in (("forced", self.forced), ("sdh", self.sdh)) if on
        ]
        return f"{self.language} ({', '.join(flags)})" if flags else self.language

    @property
    def variant_key(self) -> tuple[str | None, bool, bool]:
        """Identity of this subtitle among siblings for the same video."""
        return (self.language, self.forced, self.sdh)


def _normalize_token(token: str) -> tuple[str, str | None]:
    """Split ``pt-BR`` into its lowercased base and region."""
    base, _, region = token.partition("-")
    base = unicodedata.normalize("NFKC", base).casefold()
    region = region.casefold() or None
    return base, region


def parse_subtitle_tags(path: Path | str) -> SubtitleTags:
    """Peel language and flag tags off the end of a subtitle filename."""
    stem = Path(path).stem if isinstance(path, str) else path.stem
    stem = unicodedata.normalize("NFKC", stem)

    language: str | None = None
    region: str | None = None
    forced = False
    sdh = False
    # "hi" is both Hindi and a hearing-impaired marker. Tags are peeled
    # right-to-left, so when we meet it we cannot yet know whether a real
    # language follows further left. Defer the decision until the walk ends.
    pending_hi = False

    for _ in range(_MAX_TAGS):
        match = _TRAILING_TOKEN.search(stem)
        if match is None:
            break
        base, token_region = _normalize_token(match["token"])

        if base in _FORCED_TOKENS:
            forced = True
        elif base in _SDH_TOKENS:
            sdh = True
        elif base == "hi" and language is None and not pending_hi:
            pending_hi = True
        elif base in _LANGUAGES and language is None:
            language = _LANGUAGES[base]
            region = token_region if token_region in _REGIONS else None
        elif base in _REGIONS and language is None and region is None:
            # A bare region ahead of the language, as in "ep.br.pt.srt".
            region = base
        else:
            break

        stem = stem[: match.start()]

    if pending_hi:
        # A language to its left means the "hi" was the marker, not Hindi.
        if language is None:
            language = "hi"
        else:
            sdh = True

    if language is not None and region is not None:
        language = f"{language}-{region}"

    return SubtitleTags(language=language, forced=forced, sdh=sdh, base_stem=stem)


def strip_tags(path: Path | str) -> str:
    """The subtitle stem with its trailing language/flag tags removed."""
    return parse_subtitle_tags(path).base_stem
