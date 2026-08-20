"""Video/subtitle pairing."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ffsubsyncui.core.matcher import (
    build_subtitle_groups,
    extract_identifier,
    normalize_name,
    propose_matches,
)
from ffsubsyncui.core.models import MatchMethod
from ffsubsyncui.core.scanner import scan_media, scan_subtitles

# The media_tree fixture also takes a keyword-only `subfolder`.
Tree = Callable[..., Path]


def test_extracts_episode_identifiers() -> None:
    assert extract_identifier("Show.S01E04.1080p") == "s1e4"
    assert extract_identifier("Show.1x04") == "s1e4"
    assert extract_identifier("Show Season 1 Episode 4") == "s1e4"
    assert extract_identifier("Daily.Show.2021.03.09") == "d20210309"
    assert extract_identifier("Just A Movie") is None


def test_normalize_strips_release_noise() -> None:
    assert normalize_name("Show.S01E01.1080p.BluRay.x264-GRP") == "show s01e01 grp"


def test_identifier_match_beats_differing_naming_styles(media_tree: Tree) -> None:
    folder = media_tree(
        [
            "Some.Show.S01E01.1080p.BluRay.x264-GRP.mkv",
            "some show s01e01.en.srt",
        ]
    )
    plan = propose_matches(scan_media(folder), scan_subtitles(folder))
    assert len(plan.matches) == 1
    match = plan.matches[0]
    assert match.method is MatchMethod.IDENTIFIER
    assert match.confidence == 100.0
    assert match.subtitle is not None
    assert match.subtitle.name == "some show s01e01.en.srt"


def test_one_video_claims_every_language_variant(media_tree: Tree) -> None:
    """The gap the proof of concept had: matching was strictly 1:1."""
    folder = media_tree(
        [
            "Show.S01E01.mkv",
            "Show.S01E01.en.srt",
            "Show.S01E01.es.srt",
            "Show.S01E01.en.forced.srt",
        ]
    )
    plan = propose_matches(scan_media(folder), scan_subtitles(folder))
    assert len(plan.matches) == 3
    assert {match.video.name for match in plan.matches} == {"Show.S01E01.mkv"}
    assert {match.language for match in plan.matches} == {
        "en",
        "es",
        "en (forced)",
    }
    assert not plan.unmatched_subtitles


def test_unmatched_video_is_reported_not_dropped(media_tree: Tree) -> None:
    folder = media_tree(["Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E01.en.srt"])
    plan = propose_matches(scan_media(folder), scan_subtitles(folder))
    unmatched = [match for match in plan.matches if not match.matched]
    assert len(unmatched) == 1
    assert unmatched[0].video.name == "Show.S01E02.mkv"
    assert unmatched[0].method is MatchMethod.UNMATCHED
    assert unmatched[0].reasons


def test_orphan_subtitles_are_surfaced(media_tree: Tree) -> None:
    folder = media_tree(
        ["Show.S01E01.mkv", "Show.S01E01.en.srt", "Other.S09E09.en.srt"]
    )
    plan = propose_matches(scan_media(folder), scan_subtitles(folder))
    assert [path.name for path in plan.unmatched_subtitles] == ["Other.S09E09.en.srt"]


def test_ambiguous_identifier_is_not_locked(media_tree: Tree) -> None:
    """Two videos claiming s1e1 means the key proves nothing."""
    folder = media_tree(
        [
            "Show.S01E01.PROPER.mkv",
            "Show.S01E01.REPACK.mkv",
            "Show.S01E01.en.srt",
        ]
    )
    plan = propose_matches(scan_media(folder), scan_subtitles(folder))
    locked = [m for m in plan.matches if m.method is MatchMethod.IDENTIFIER]
    assert not locked


def test_previously_synced_output_is_not_offered_as_input(media_tree: Tree) -> None:
    """Re-scanning a folder must not queue our own output for a second pass."""
    folder = media_tree(
        ["Show.S01E01.mkv", "Show.S01E01.en.srt", "Show.S01E01.en.synced.srt"]
    )
    names = {path.name for path in scan_subtitles(folder)}
    assert names == {"Show.S01E01.en.srt"}


def test_fuzzy_match_flags_a_close_runner_up(media_tree: Tree) -> None:
    folder = media_tree(
        [
            "The Big Show 2020.mkv",
            "The Big Show 2020 Part One.srt",
            "The Big Show 2020 Part Two.srt",
        ]
    )
    plan = propose_matches(scan_media(folder), scan_subtitles(folder), threshold=50.0)
    fuzzy = [m for m in plan.matches if m.method is MatchMethod.FUZZY]
    assert fuzzy
    assert any("runner-up" in reason for match in fuzzy for reason in match.reasons)


def test_threshold_rejects_weak_pairings(media_tree: Tree) -> None:
    folder = media_tree(["Completely Different Film.mkv", "Unrelated Show Name.srt"])
    plan = propose_matches(scan_media(folder), scan_subtitles(folder), threshold=99.0)
    assert not plan.matched


def test_groups_collapse_language_variants(media_tree: Tree) -> None:
    folder = media_tree(["ep.en.srt", "ep.es.srt", "ep.fr.srt", "other.en.srt"])
    groups = build_subtitle_groups(scan_subtitles(folder))
    sizes = sorted(len(group.entries) for group in groups)
    assert sizes == [1, 3]


def test_recursive_scan_finds_nested_files(media_tree: Tree) -> None:
    root = media_tree(["Show.S01E01.mkv"])
    media_tree(["Show.S01E01.en.srt"], subfolder="subs")
    plan = propose_matches(scan_media(root), scan_subtitles(root))
    assert plan.matched


def test_subtitle_references_can_be_scanned(media_tree: Tree) -> None:
    """Aligning against a known-good subtitle needs no ffmpeg at all."""
    from ffsubsyncui.core.scanner import scan_references

    folder = media_tree(["Show.S01E01.mkv", "Show.S01E01.en.srt", "Show.S01E01.es.srt"])
    assert [p.name for p in scan_references(folder)] == ["Show.S01E01.mkv"]
    as_subtitles = scan_references(folder, subtitles_as_reference=True)
    assert {p.name for p in as_subtitles} == {
        "Show.S01E01.en.srt",
        "Show.S01E01.es.srt",
    }
