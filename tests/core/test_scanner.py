from pathlib import Path

from subsyncr.core.scanner import scan_subtitles


def test_crashed_workers_staging_is_not_offered_as_input(tmp_path: Path):
    original = tmp_path / "film.srt"
    original.write_bytes(b"original")
    for name in (".subsyncr-output-abcd", ".subsyncr-backup-efgh"):
        staging = tmp_path / name
        staging.mkdir()
        (staging / "film.srt").write_bytes(b"partial")
    assert scan_subtitles(tmp_path) == [original]
