from __future__ import annotations

from pathlib import Path

from ffsubsyncui.domain import MatchMethod
from ffsubsyncui.matching import extract_identifier, normalize_name, propose_matches


def test_extracts_common_episode_and_date_identifiers() -> None:
    assert extract_identifier(Path("Show.S01E02.1080p.mkv")) == "s1e2"
    assert extract_identifier(Path("Show 1x03.srt")) == "s1e3"
    assert extract_identifier(Path("Daily.Show.2026-08-19.mkv")) == "d20260819"


def test_normalization_removes_release_noise() -> None:
    left = normalize_name(Path("Some.Show.S01E02.1080p.WEB-DL.x265.mkv"))
    right = normalize_name(Path("Some Show S01E02 English.srt"))
    assert left == right


def test_identifier_matching_wins_over_similar_series_names() -> None:
    references = [Path("Show.S01E01.mkv"), Path("Show.S01E02.mkv")]
    subtitles = [Path("show 01x02 eng.srt"), Path("show 01x01 eng.srt")]
    matches = propose_matches(references, subtitles)
    assert [match.subtitle_path for match in matches] == [subtitles[1], subtitles[0]]
    assert all(match.method is MatchMethod.REGEX for match in matches)


def test_fuzzy_matching_is_one_to_one_and_deterministic() -> None:
    references = [Path("Movie Alpha 2025.mkv"), Path("Movie Beta 2025.mkv")]
    subtitles = [Path("Movie.Beta.2025.en.srt"), Path("Movie.Alpha.2025.en.srt")]
    first = propose_matches(references, subtitles, threshold=60)
    second = propose_matches(
        list(reversed(references)), list(reversed(subtitles)), threshold=60
    )
    assert first == second
    assert len({match.subtitle_path for match in first}) == 2


def test_low_score_reference_remains_unmatched() -> None:
    [proposal] = propose_matches(
        [Path("Completely Different.mkv")], [Path("No Relation.srt")], threshold=95
    )
    assert proposal.subtitle_path is None
    assert proposal.method is MatchMethod.UNMATCHED
