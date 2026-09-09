"""Format conversion and charset detection.

These exist so an engine's format limits never become the user's problem: a
.vtt subtitle can be synced with alass, which cannot read one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from subsyncr.core import encoding as encodings, subtitles

_SRT = """1
00:00:10,000 --> 00:00:12,000
Hello there.

2
00:00:20,500 --> 00:00:22,000
General Kenobi.
"""


def _srt(tmp_path: Path, name: str = "ep.srt", text: str = _SRT) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# -- encoding ------------------------------------------------------------


def test_utf8_is_detected(tmp_path: Path) -> None:
    assert encodings.detect(_srt(tmp_path)) == "utf-8"


def test_valid_utf8_wins_over_a_statistical_guess(tmp_path: Path) -> None:
    """Short non-ASCII text reads convincingly as several legacy codepages."""
    path = tmp_path / "short.srt"
    path.write_text(_SRT.replace("Hello there.", "Привет"), encoding="utf-8")
    assert encodings.detect(path) == "utf-8"
    assert "Привет" in path.read_text(encoding=encodings.detect(path))


def test_a_byte_order_mark_is_honoured(tmp_path: Path) -> None:
    path = tmp_path / "bom.srt"
    path.write_text(_SRT, encoding="utf-8-sig")
    assert encodings.detect(path) == "utf-8-sig"
    # The codec that strips the BOM is the one a parser wants.
    assert path.read_text(encoding=encodings.detect(path)).startswith("1")


def test_the_utf8_fast_path_survives_a_sliced_character(tmp_path: Path) -> None:
    """Detection samples a fixed number of bytes, which cuts a character in half.

    Any subtitle past that size hits this, and a feature-length CJK .srt is
    well past it. Rejecting the sliced tail would drop a plainly UTF-8 file
    into statistical detection for no reason.
    """
    unit = "1\n00:00:01,000 --> 00:00:03,000\n這是一個中文字幕。\n\n"
    for shift in range(4):
        # One of these four offsets puts the cut mid-character; all must agree.
        path = tmp_path / f"long{shift}.srt"
        path.write_bytes(b"\n" * shift + (unit * 3000).encode("utf-8"))
        assert path.stat().st_size > encodings._SAMPLE_BYTES
        assert encodings.detect(path) == "utf-8"


def test_detection_samples_the_head_rather_than_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 30 MB subtitle must not cost 30 MB of reading to characterise."""
    path = tmp_path / "big.srt"
    path.write_bytes(b"a" * (encodings._SAMPLE_BYTES * 4))
    sizes: list[int] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_open(self, *args, **kwargs)
        if self == path:
            real_read = handle.read

            def counted(size=-1):  # type: ignore[no-untyped-def]
                data = real_read(size)
                sizes.append(len(data))
                return data

            handle.read = counted  # type: ignore[method-assign]
        return handle

    encodings._detect.cache_clear()
    monkeypatch.setattr(Path, "open", spy)
    try:
        assert encodings.detect(path) == "utf-8"
    finally:
        encodings._detect.cache_clear()
    assert sizes and max(sizes) <= encodings._SAMPLE_BYTES


def test_a_legacy_cyrillic_subtitle_is_not_read_as_utf8(tmp_path: Path) -> None:
    """The case that turns dialogue into mojibake if we guess wrong."""
    path = tmp_path / "ru.srt"
    body = _SRT.replace("Hello there.", "Привет, генерал Кеноби, как дела сегодня")
    path.write_bytes(body.encode("cp1251"))
    detected = encodings.detect(path)
    assert detected != "utf-8"
    # And the detected codec actually decodes it.
    assert "Привет" in path.read_text(encoding=detected)


def test_an_explicit_encoding_overrides_detection(tmp_path: Path) -> None:
    path = _srt(tmp_path)
    assert encodings.resolve(path, "cp1252") == "cp1252"


def test_infer_and_its_synonyms_mean_detect(tmp_path: Path) -> None:
    path = _srt(tmp_path)
    for word in ("infer", "auto", "detect", "", "  "):
        assert encodings.resolve(path, word) == encodings.detect(path)


def test_pure_ascii_is_reported_as_utf8() -> None:
    """Saying ASCII would make a later non-ASCII edit unreadable."""
    assert encodings.normalize("ascii") == "utf-8"


def test_detection_of_a_missing_file_falls_back(tmp_path: Path) -> None:
    assert encodings.detect(tmp_path / "gone.srt") == encodings.FALLBACK


# -- conversion ----------------------------------------------------------


def test_srt_converts_to_vtt_and_back(tmp_path: Path) -> None:
    source = _srt(tmp_path)
    vtt = subtitles.convert(source, tmp_path / "ep.vtt")
    assert vtt.is_file()
    assert vtt.read_text(encoding="utf-8").lstrip().startswith("WEBVTT")

    back = subtitles.convert(vtt, tmp_path / "round.srt")
    assert "General Kenobi." in back.read_text(encoding="utf-8")


def test_timing_survives_a_round_trip(tmp_path: Path) -> None:
    """A conversion that shifted timings would silently ruin the sync."""
    import pysubs2

    source = _srt(tmp_path)
    original = pysubs2.load(str(source))
    ass = subtitles.convert(source, tmp_path / "ep.ass")
    restored = pysubs2.load(str(subtitles.convert(ass, tmp_path / "back.srt")))
    assert [(e.start, e.end) for e in restored] == [(e.start, e.end) for e in original]


def test_image_subtitles_are_refused_rather_than_mangled(tmp_path: Path) -> None:
    idx = tmp_path / "ep.idx"
    idx.write_bytes(b"not text")
    with pytest.raises(subtitles.ConversionError):
        subtitles.convert(idx, tmp_path / "ep.srt")


def test_an_unknown_format_is_refused(tmp_path: Path) -> None:
    odd = tmp_path / "ep.xyz"
    odd.write_text("nonsense", encoding="utf-8")
    with pytest.raises(subtitles.ConversionError):
        subtitles.convert(odd, tmp_path / "ep.srt")


def test_a_legacy_encoding_is_converted_to_utf8(tmp_path: Path) -> None:
    path = tmp_path / "ru.srt"
    # Enough Cyrillic for detection to have something to work with. A couple of
    # words is genuinely ambiguous between the single-byte codepages.
    russian = "Привет, генерал Кеноби, как ваши дела сегодня вечером"
    path.write_bytes(_SRT.replace("Hello there.", russian).encode("cp1251"))
    converted = subtitles.convert(path, tmp_path / "out.vtt")
    assert "Привет" in converted.read_text(encoding="utf-8")


# -- preparing a run -----------------------------------------------------


def test_nothing_is_copied_when_the_engine_already_fits(tmp_path: Path) -> None:
    subtitle = _srt(tmp_path)
    plan = subtitles.prepare(
        "ep.mkv",
        subtitle,
        tmp_path / "out.srt",
        accepted={".srt", ".ass"},
        writable={".srt", ".ass"},
        workdir=tmp_path,
    )
    assert plan.subtitle == subtitle
    assert plan.output == plan.final_output
    assert not plan.temporary


def test_an_unreadable_subtitle_is_converted_for_the_engine(tmp_path: Path) -> None:
    """autosubsync takes SubRip only; a .vtt would otherwise be refused."""
    source = subtitles.convert(_srt(tmp_path), tmp_path / "ep.vtt")
    plan = subtitles.prepare(
        "ep.mkv",
        source,
        tmp_path / "out.srt",
        accepted={".srt"},
        writable={".srt"},
        workdir=tmp_path,
    )
    assert plan.subtitle != source
    assert plan.subtitle.suffix == ".srt"
    assert plan.subtitle.is_file()
    plan.clean()
    assert not plan.subtitle.exists()


def test_an_unwritable_output_format_is_converted_back(tmp_path: Path) -> None:
    subtitle = _srt(tmp_path)
    wanted = tmp_path / "out.vtt"
    plan = subtitles.prepare(
        "ep.mkv",
        subtitle,
        wanted,
        accepted={".srt"},
        writable={".srt"},
        workdir=tmp_path,
    )
    assert plan.needs_conversion_back
    assert plan.output.suffix == ".srt"

    # The engine writes SubRip; the user still gets WebVTT.
    plan.output.write_text(_SRT, encoding="utf-8")
    subtitles.finish(plan)
    assert wanted.read_text(encoding="utf-8").lstrip().startswith("WEBVTT")
    plan.clean()


def test_a_subtitle_reference_is_converted_too(tmp_path: Path) -> None:
    reference = subtitles.convert(_srt(tmp_path), tmp_path / "ref.vtt")
    plan = subtitles.prepare(
        str(reference),
        _srt(tmp_path, "in.srt"),
        tmp_path / "out.srt",
        accepted={".srt"},
        writable={".srt"},
        workdir=tmp_path,
        reference_is_subtitle=True,
    )
    assert plan.reference != str(reference)
    assert Path(plan.reference).suffix == ".srt"
    plan.clean()


def test_an_image_reference_is_left_alone(tmp_path: Path) -> None:
    """Only alass reads VobSub, and converting it would destroy it."""
    reference = tmp_path / "ref.idx"
    reference.write_bytes(b"bitmaps")
    plan = subtitles.prepare(
        str(reference),
        _srt(tmp_path),
        tmp_path / "out.srt",
        accepted={".srt"},
        writable={".srt"},
        workdir=tmp_path,
        reference_is_subtitle=True,
    )
    assert plan.reference == str(reference)
