"""Choosing which embedded subtitle track to align against."""

from __future__ import annotations

from pathlib import Path

from subsyncr.core import embedded
from subsyncr.core.embedded import Track


def _track(**overrides: object) -> Track:
    fields: dict[str, object] = {
        "index": 0,
        "codec": "subrip",
        "language": "eng",
        "title": "",
        "forced": False,
        "default": False,
        "frames": 0,
    }
    fields.update(overrides)
    return Track(**fields)  # type: ignore[arg-type]


def test_nothing_is_chosen_when_there_are_no_tracks() -> None:
    assert embedded.choose([]) is None


def test_image_tracks_are_never_chosen() -> None:
    """PGS and VobSub are bitmaps; there are no timestamps to compare."""
    picture = [
        _track(index=0, codec="hdmv_pgs_subtitle"),
        _track(index=1, codec="dvd_subtitle"),
    ]
    assert embedded.choose(picture) is None
    assert not picture[0].is_text


def test_a_language_match_outranks_everything_else() -> None:
    chosen = embedded.choose(
        [
            _track(index=0, language="eng", default=True, frames=900),
            _track(index=1, language="spa", frames=10),
        ],
        prefer_language="es",
    )
    assert chosen is not None
    assert chosen.language == "spa"


def test_a_forced_track_loses_to_a_full_one() -> None:
    """Forced tracks are only the foreign lines - a terrible reference."""
    chosen = embedded.choose(
        [
            _track(index=0, language="eng", forced=True, default=True),
            _track(index=1, language="eng", forced=False),
        ],
        prefer_language="en",
    )
    assert chosen is not None
    assert chosen.index == 1


def test_the_fuller_track_wins_between_equals() -> None:
    chosen = embedded.choose(
        [
            _track(index=0, language="eng", frames=120),
            _track(index=1, language="eng", frames=1400),
        ]
    )
    assert chosen is not None
    assert chosen.index == 1


def test_a_two_letter_language_matches_a_three_letter_tag() -> None:
    """Subtitle filenames say "en"; containers say "eng"."""
    chosen = embedded.choose(
        [_track(index=0, language="spa"), _track(index=1, language="eng")],
        prefer_language="en",
    )
    assert chosen is not None
    assert chosen.language == "eng"


def test_a_text_track_is_still_chosen_without_a_language_preference() -> None:
    chosen = embedded.choose([_track(index=0, language="", default=True)])
    assert chosen is not None


def test_probing_something_that_is_not_media_finds_nothing(tmp_path: Path) -> None:
    junk = tmp_path / "notes.bin"
    junk.write_bytes(b"\x00\x01 not a container at all")
    assert embedded.probe_tracks(junk) == []


def test_three_letter_container_tags_match_two_letter_preferences() -> None:
    """es/spa and ja/jpn share no prefix, so this cannot be a startswith test."""
    for short, long in (("es", "spa"), ("ja", "jpn"), ("zh", "chi"), ("de", "ger")):
        chosen = embedded.choose(
            [_track(index=0, language="eng"), _track(index=1, language=long)],
            prefer_language=short,
        )
        assert chosen is not None
        assert chosen.language == long, f"{short} should have matched {long}"


def test_pgs_choice_skips_text_and_forced_tracks() -> None:
    """ffsubsync's own auto-detect would take the forced track here."""
    tracks = [
        _track(index=0, codec="subrip"),
        _track(index=1, codec="hdmv_pgs_subtitle", forced=True),
        _track(index=2, codec="hdmv_pgs_subtitle"),
    ]
    chosen = embedded.choose_pgs(tracks)
    assert chosen is not None
    assert chosen.index == 2


def test_no_pgs_track_means_no_pgs_choice() -> None:
    tracks = [_track(index=0, codec="subrip"), _track(index=1, codec="dvd_subtitle")]
    assert embedded.choose_pgs(tracks) is None
