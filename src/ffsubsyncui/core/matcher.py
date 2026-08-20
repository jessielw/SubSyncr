"""Pair video files with subtitle files by filename.

Three tiers, strictest first. Each tier only locks a pairing when it is
*unique* on both sides, so an ambiguous key never produces a confident match.

Unlike a naive pairing, the unit of matching is a video against a *group* of
subtitles that differ only by language tag. One video legitimately owns
``ep.en.srt``, ``ep.es.srt`` and ``ep.en.forced.srt`` at once.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import re
import unicodedata

from rapidfuzz import fuzz

from ffsubsyncui.core.language import SubtitleTags, parse_subtitle_tags
from ffsubsyncui.core.models import MatchMethod, MatchResult

DEFAULT_THRESHOLD = 75.0

# How close the runner-up may be before a fuzzy match is flagged for review.
_AMBIGUITY_MARGIN = 5.0

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
    r"webrip|hdtv|dvdrip|dvd|remux|x26[45]|h26[45]|hevc|avc|av1|aac|ac3|eac3|"
    r"dts(?:[ ._-]?hd)?(?:[ ._-]?ma)?|truehd|atmos|flac|opus|"
    r"proper|repack|internal|limited|extended|uncut|unrated|amzn|nf|dsnp|hmax|"
    r"atvp|pcok|stan|hdr10\+?|hdr|dv|dolby[ ._-]?vision|sdr|10bit|8bit)\b"
)


def extract_identifier(text: str) -> str | None:
    """A season/episode or air-date key, if the name carries one."""
    normalized = unicodedata.normalize("NFKC", text)
    for pattern in _IDENTIFIER_PATTERNS:
        if match := pattern.search(normalized):
            return f"s{int(match['season'])}e{int(match['episode'])}"
    if match := _DATE_PATTERN.search(normalized):
        return (
            f"d{int(match['year']):04d}{int(match['month']):02d}{int(match['day']):02d}"
        )
    return None


def normalize_name(text: str) -> str:
    """Strip release noise and punctuation so two names can be compared."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _NOISE_PATTERN.sub(" ", normalized)
    normalized = re.sub(r"[\[\](){}]", " ", normalized)
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


@dataclass(slots=True)
class SubtitleGroup:
    """Subtitles that describe the same content in different languages."""

    key: str
    identifier: str | None
    entries: list[tuple[Path, SubtitleTags]] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        return [path for path, _ in self.entries]

    @property
    def sample(self) -> Path:
        return self.entries[0][0]


@dataclass(frozen=True, slots=True)
class MatchPlan:
    """Everything the review UI needs to show a proposed batch."""

    matches: tuple[MatchResult, ...]
    unmatched_subtitles: tuple[Path, ...]

    @property
    def matched(self) -> tuple[MatchResult, ...]:
        return tuple(match for match in self.matches if match.matched)

    @property
    def unmatched_videos(self) -> tuple[Path, ...]:
        return tuple(match.video for match in self.matches if not match.matched)


def build_subtitle_groups(subtitles: Iterable[Path]) -> list[SubtitleGroup]:
    """Collapse subtitles into one group per underlying episode/title."""
    groups: dict[str, SubtitleGroup] = {}
    for path in sorted(set(subtitles), key=lambda item: str(item).casefold()):
        tags = parse_subtitle_tags(path)
        # Fall back to the raw stem when tag stripping consumed everything.
        base = tags.base_stem or path.stem
        key = normalize_name(base) or path.stem.casefold()
        group = groups.get(key)
        if group is None:
            group = SubtitleGroup(key=key, identifier=extract_identifier(base))
            groups[key] = group
        group.entries.append((path, tags))
    return [groups[key] for key in sorted(groups)]


def _unique_by[T](
    items: Sequence[T], key_func: Callable[[T], str | None]
) -> dict[str, T]:
    """Map key -> item, keeping only keys claimed by exactly one item."""
    buckets: defaultdict[str, list[T]] = defaultdict(list)
    for item in items:
        if key := key_func(item):
            buckets[key].append(item)
    return {key: values[0] for key, values in buckets.items() if len(values) == 1}


def _results_for(
    video: Path,
    group: SubtitleGroup,
    method: MatchMethod,
    confidence: float,
    reasons: tuple[str, ...],
) -> list[MatchResult]:
    shared = reasons
    if len(group.entries) > 1:
        shared += (f"{len(group.entries)} subtitle variants share this video",)
    return [
        MatchResult(
            video=video,
            subtitle=path,
            confidence=confidence,
            method=method,
            language=tags.label or None,
            reasons=shared,
        )
        for path, tags in group.entries
    ]


def propose_matches(
    videos: Iterable[Path],
    subtitles: Iterable[Path],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> MatchPlan:
    """Propose a video/subtitle pairing for every video supplied."""
    ordered_videos = sorted(set(videos), key=lambda path: str(path).casefold())
    groups = build_subtitle_groups(subtitles)

    claimed_videos: set[Path] = set()
    claimed_groups: set[str] = set()
    results: dict[Path, list[MatchResult]] = {}

    def lock(
        video_key: Callable[[Path], str | None],
        group_key: Callable[[SubtitleGroup], str | None],
        method: MatchMethod,
        reason: Callable[[str], str],
    ) -> None:
        free_videos = [path for path in ordered_videos if path not in claimed_videos]
        free_groups = [group for group in groups if group.key not in claimed_groups]
        video_map = _unique_by(free_videos, video_key)
        group_map = _unique_by(free_groups, group_key)
        for key in sorted(video_map.keys() & group_map.keys()):
            video = video_map[key]
            group = group_map[key]
            claimed_videos.add(video)
            claimed_groups.add(group.key)
            results[video] = _results_for(video, group, method, 100.0, (reason(key),))

    lock(
        lambda path: extract_identifier(path.stem),
        lambda group: group.identifier,
        MatchMethod.IDENTIFIER,
        lambda key: f"episode identifier {key} is unique on both sides",
    )
    lock(
        lambda path: normalize_name(path.stem),
        lambda group: group.key,
        MatchMethod.EXACT,
        lambda _: "normalized filenames are identical",
    )

    # Tier 3: fuzzy. Greedy, highest score first, still 1:1 video<->group.
    free_videos = [path for path in ordered_videos if path not in claimed_videos]
    free_groups = [group for group in groups if group.key not in claimed_groups]
    scores: dict[tuple[Path, str], float] = {}
    for video in free_videos:
        left = normalize_name(video.stem)
        for group in free_groups:
            scores[(video, group.key)] = max(
                fuzz.WRatio(left, group.key),
                fuzz.token_sort_ratio(left, group.key),
                fuzz.token_set_ratio(left, group.key),
            )

    per_video: defaultdict[Path, list[float]] = defaultdict(list)
    for (video, _), score in scores.items():
        per_video[video].append(score)
    for ranking in per_video.values():
        ranking.sort(reverse=True)

    by_key = {group.key: group for group in free_groups}
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], str(item[0][0]).casefold(), item[0][1]),
    )
    for (video, group_key), score in ranked:
        if score < threshold or video in claimed_videos or group_key in claimed_groups:
            continue
        reasons = [f"name similarity {score:.0f}%"]
        ranking = per_video[video]
        if len(ranking) > 1 and ranking[0] - ranking[1] < _AMBIGUITY_MARGIN:
            reasons.append(f"runner-up scored {ranking[1]:.0f}% - verify this pairing")
        claimed_videos.add(video)
        claimed_groups.add(group_key)
        results[video] = _results_for(
            video, by_key[group_key], MatchMethod.FUZZY, score, tuple(reasons)
        )

    matches: list[MatchResult] = []
    for video in ordered_videos:
        matches.extend(
            results.get(
                video,
                [
                    MatchResult(
                        video=video,
                        subtitle=None,
                        confidence=0.0,
                        method=MatchMethod.UNMATCHED,
                        reasons=("no candidate reached the similarity threshold",),
                    )
                ],
            )
        )

    leftovers = tuple(
        path
        for group in groups
        if group.key not in claimed_groups
        for path in group.paths
    )
    return MatchPlan(matches=tuple(matches), unmatched_subtitles=leftovers)
