"""Language tag extraction from subtitle filenames."""

from __future__ import annotations

from pathlib import Path

import pytest

from subsyncr.core.language import parse_subtitle_tags, strip_tags


@pytest.mark.parametrize(
    ("name", "language", "forced", "sdh", "base"),
    [
        ("ep.en.srt", "en", False, False, "ep"),
        ("ep.eng.srt", "en", False, False, "ep"),
        ("ep.English.srt", "en", False, False, "ep"),
        ("Show.S01E01.es.srt", "es", False, False, "Show.S01E01"),
        ("Show.S01E01.spanish.srt", "es", False, False, "Show.S01E01"),
        ("Show.S01E01.en.forced.srt", "en", True, False, "Show.S01E01"),
        ("Show.S01E01.eng.sdh.srt", "en", False, True, "Show.S01E01"),
        ("Show.S01E01.pt-BR.srt", "pt-br", False, False, "Show.S01E01"),
        ("Show.S01E01.pt-BR.sdh.srt", "pt-br", False, True, "Show.S01E01"),
        ("Show.S01E01.forced.srt", None, True, False, "Show.S01E01"),
        # No tag at all: the stem must survive untouched.
        ("Show.S01E01.srt", None, False, False, "Show.S01E01"),
    ],
)
def test_parses_tags(
    name: str, language: str | None, forced: bool, sdh: bool, base: str
) -> None:
    tags = parse_subtitle_tags(Path(name))
    assert tags.language == language
    assert tags.forced is forced
    assert tags.sdh is sdh
    assert tags.base_stem == base


def test_release_tokens_are_not_mistaken_for_languages() -> None:
    """A quality tag must not be eaten as if it were a language."""
    tags = parse_subtitle_tags(Path("Show.S01E01.1080p.BluRay.x264-GRP.srt"))
    assert tags.language is None
    assert tags.base_stem == "Show.S01E01.1080p.BluRay.x264-GRP"


def test_hi_is_hindi_alone_but_hearing_impaired_after_a_language() -> None:
    """ "hi" is genuinely ambiguous; position decides."""
    assert parse_subtitle_tags(Path("ep.hi.srt")).language == "hi"
    second = parse_subtitle_tags(Path("ep.en.hi.srt"))
    assert second.language == "en"
    assert second.sdh is True


def test_variant_key_separates_siblings() -> None:
    keys = {
        parse_subtitle_tags(Path(name)).variant_key
        for name in ("ep.en.srt", "ep.es.srt", "ep.en.forced.srt", "ep.en.sdh.srt")
    }
    assert len(keys) == 4


def test_strip_tags_matches_base_stem() -> None:
    assert strip_tags("Show.S01E01.en.forced.srt") == "Show.S01E01"


def test_tag_peeling_is_bounded() -> None:
    """A stem made only of tag-like words must not be reduced to nothing."""
    tags = parse_subtitle_tags(Path("en.es.fr.de.it.srt"))
    assert tags.base_stem != ""
