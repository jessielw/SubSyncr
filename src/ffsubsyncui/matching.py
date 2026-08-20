from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import unicodedata

from rapidfuzz import fuzz

from ffsubsyncui.domain import MatchMethod, MatchProposal

MEDIA_EXTENSIONS = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"}
)
SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".sub"})

_IDENTIFIER_PATTERNS = (
    re.compile(r"(?i)(?<!\w)s(?P<season>\d{1,3})[ ._-]*e(?P<episode>\d{1,4})(?!\d)"),
    re.compile(r"(?i)(?<!\d)(?P<season>\d{1,3})x(?P<episode>\d{1,4})(?!\d)"),
    re.compile(
        r"(?i)\bseason[ ._-]*(?P<season>\d{1,3})"
        r"[ ._-]*episode[ ._-]*(?P<episode>\d{1,4})\b"
    ),
)
_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})[ ._-]"
    r"(?P<month>0?[1-9]|1[0-2])[ ._-]"
    r"(?P<day>0?[1-9]|[12]\d|3[01])(?!\d)"
)
_NOISE_PATTERN = re.compile(
    r"(?i)\b(?:2160p|1080p|720p|576p|480p|uhd|bluray|blu[ ._-]?ray|web[ ._-]?dl|"
    r"webrip|hdtv|dvdrip|remux|x26[45]|h26[45]|hevc|av1|aac|ac3|eac3|dts(?:[ ._-]?hd)?|"
    r"proper|repack|internal|limited|extended|amzn|nf|dsnp|hmax|atvp|hdr10\+?|dv|"
    r"dolby[ ._-]?vision|"
    r"forced|sdh|cc|eng|english|multi)\b"
)


def scan_folder(
    folder: Path, extensions: frozenset[str], *, recursive: bool
) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(
        (
            path
            for path in iterator
            if path.is_file() and path.suffix.lower() in extensions
        ),
        key=lambda path: str(path).casefold(),
    )


def extract_identifier(path: Path) -> str | None:
    stem = unicodedata.normalize("NFKC", path.stem)
    for pattern in _IDENTIFIER_PATTERNS:
        if match := pattern.search(stem):
            return f"s{int(match['season'])}e{int(match['episode'])}"
    if match := _DATE_PATTERN.search(stem):
        return (
            f"d{int(match['year']):04d}{int(match['month']):02d}{int(match['day']):02d}"
        )
    return None


def normalize_name(path: Path) -> str:
    text = unicodedata.normalize("NFKC", path.stem).casefold()
    text = _NOISE_PATTERN.sub(" ", text)
    text = re.sub(r"[\[\](){}]", " ", text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _unique_groups(paths: list[Path], key_func: object) -> dict[str, Path]:
    groups: defaultdict[str, list[Path]] = defaultdict(list)
    for path in paths:
        key = key_func(path)  # type: ignore[operator]
        if key:
            groups[str(key)].append(path)
    return {key: values[0] for key, values in groups.items() if len(values) == 1}


def propose_matches(
    references: list[Path], subtitles: list[Path], *, threshold: float = 75.0
) -> list[MatchProposal]:
    refs = sorted(set(references), key=lambda path: str(path).casefold())
    subs = sorted(set(subtitles), key=lambda path: str(path).casefold())
    assigned_refs: set[Path] = set()
    assigned_subs: set[Path] = set()
    proposals: dict[Path, MatchProposal] = {}

    def lock_unique(key_func: object, method: MatchMethod) -> None:
        ref_map = _unique_groups(
            [path for path in refs if path not in assigned_refs], key_func
        )
        sub_map = _unique_groups(
            [path for path in subs if path not in assigned_subs], key_func
        )
        for key in sorted(ref_map.keys() & sub_map.keys()):
            reference = ref_map[key]
            subtitle = sub_map[key]
            assigned_refs.add(reference)
            assigned_subs.add(subtitle)
            proposals[reference] = MatchProposal(reference, subtitle, method, 100.0)

    lock_unique(extract_identifier, MatchMethod.REGEX)
    lock_unique(normalize_name, MatchMethod.EXACT)

    remaining_refs = [path for path in refs if path not in assigned_refs]
    remaining_subs = [path for path in subs if path not in assigned_subs]
    scores: dict[tuple[Path, Path], float] = {}
    for reference in remaining_refs:
        left = normalize_name(reference)
        for subtitle in remaining_subs:
            right = normalize_name(subtitle)
            scores[(reference, subtitle)] = max(
                fuzz.WRatio(left, right),
                fuzz.token_sort_ratio(left, right),
                fuzz.token_set_ratio(left, right),
            )

    ranked = sorted(
        scores.items(),
        key=lambda item: (
            -item[1],
            str(item[0][0]).casefold(),
            str(item[0][1]).casefold(),
        ),
    )
    ref_rankings = {
        reference: sorted(
            (
                score
                for (candidate, _), score in scores.items()
                if candidate == reference
            ),
            reverse=True,
        )
        for reference in remaining_refs
    }
    for (reference, subtitle), score in ranked:
        if score < threshold or reference in assigned_refs or subtitle in assigned_subs:
            continue
        ranking = ref_rankings[reference]
        ambiguity = ""
        if len(ranking) > 1 and ranking[0] - ranking[1] < 5.0:
            ambiguity = "Top candidates are within 5 points; verify this match"
        assigned_refs.add(reference)
        assigned_subs.add(subtitle)
        proposals[reference] = MatchProposal(
            reference, subtitle, MatchMethod.FUZZY, score, ambiguity
        )

    for reference in refs:
        proposals.setdefault(
            reference,
            MatchProposal(
                reference,
                None,
                MatchMethod.UNMATCHED,
                0.0,
                "No unique candidate met the similarity threshold",
            ),
        )
    return [proposals[reference] for reference in refs]
