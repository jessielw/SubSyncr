"""The engine registry and the capability questions the app asks it."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ffsubsyncui.core import engines
from ffsubsyncui.core.engines import EngineId, EngineKind, Reported


def test_every_registered_engine_is_keyed_by_its_own_id() -> None:
    for engine_id, spec in ((spec.id, spec) for spec in engines.all_specs()):
        assert engines.get(engine_id) is spec


def test_ffsubsync_is_the_only_engine_that_shares_an_extraction() -> None:
    """The group design exists for this one capability; guard it explicitly."""
    sharing = [spec.id for spec in engines.all_specs() if spec.shares_reference]
    assert sharing == [EngineId.FFSUBSYNC]


def test_every_engine_id_is_implemented() -> None:
    assert {spec.id for spec in engines.all_specs()} == set(EngineId)


def test_an_unknown_name_is_a_miss_not_a_fallback() -> None:
    """``get`` must not answer questions about "nope" with ffsubsync's answers."""
    assert not engines.registered("nope")
    with pytest.raises(KeyError):
        engines.get("nope")


def test_engine_id_parsing_never_raises() -> None:
    assert EngineId.parse("alass") is EngineId.ALASS
    assert EngineId.parse("nonsense") is EngineId.FFSUBSYNC
    assert EngineId.parse(None, EngineId.LAPSE) is EngineId.LAPSE


def test_ffsubsync_reads_sub_but_will_not_write_it() -> None:
    """ffsubsync parses MicroDVD and raises NotImplementedError serializing it."""
    spec = engines.get(EngineId.FFSUBSYNC)
    assert spec.accepts_subtitle(".sub")
    assert not spec.can_write(".sub")


def test_video_references_are_accepted_regardless_of_suffix() -> None:
    spec = engines.get(EngineId.FFSUBSYNC)
    assert spec.accepts_reference(".mkv", is_subtitle=False)
    assert spec.accepts_reference(".srt", is_subtitle=True)
    # A subtitle format the engine cannot parse is not a usable reference.
    assert not spec.accepts_reference(".idx", is_subtitle=True)


def test_case_is_not_significant_in_suffixes() -> None:
    spec = engines.get(EngineId.FFSUBSYNC)
    assert spec.accepts_subtitle(".SRT")
    assert spec.can_write(".VTT")


def test_ffsubsync_reports_every_result_field() -> None:
    assert engines.get(EngineId.FFSUBSYNC).reports == {
        Reported.OFFSET,
        Reported.SCALE,
        Reported.SCORE,
    }
    assert engines.get(EngineId.FFSUBSYNC).kind is EngineKind.PYTHON


def test_registry_does_not_import_engine_implementations() -> None:
    """Importing the registry must not drag ffsubsync into the GUI process.

    Implementations name their dependencies at module scope, so the registry
    refers to them by dotted path and the worker imports them lazily.
    """
    source = Path(engines.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [
        name
        for name in imported
        if name.startswith("ffsubsyncui.core.engines.")
        and name != "ffsubsyncui.core.engines.base"
    ]
    # And every spec's module path is a real, importable-looking target.
    for spec in engines.all_specs():
        assert spec.module.startswith("ffsubsyncui.core.engines.")


# -- native engines ------------------------------------------------------


def test_native_engines_do_not_share_a_reference() -> None:
    """Each is a separate process per subtitle, so there is nothing to share."""
    for engine in (EngineId.ALASS, EngineId.LAPSE):
        spec = engines.get(engine)
        assert spec.kind is EngineKind.BINARY
        assert not spec.shares_reference


def test_alass_alone_reads_image_and_microdvd_subtitles() -> None:
    """alass takes .idx/.sub, which is why a fallback chain can be worth it."""
    alass = engines.get(EngineId.ALASS)
    assert alass.accepts_subtitle(".idx")
    assert not engines.get(EngineId.LAPSE).accepts_subtitle(".idx")
    assert not engines.get(EngineId.FFSUBSYNC).accepts_subtitle(".idx")


def test_alass_is_the_only_engine_without_a_confidence() -> None:
    """It prints a shift and a framerate ratio, but nothing to rank them by."""
    unscored = {
        spec.id for spec in engines.all_specs() if Reported.SCORE not in spec.reports
    }
    assert unscored == {EngineId.ALASS}
    assert Reported.OFFSET in engines.get(EngineId.ALASS).reports


def test_autosubsync_is_the_only_engine_that_needs_real_audio() -> None:
    """The capability a fallback chain checks before spending a process."""
    audio_only = {
        spec.id for spec in engines.all_specs() if not spec.subtitle_reference
    }
    assert audio_only == {EngineId.AUTOSUBSYNC}


def test_autosubsync_reads_subrip_and_nothing_else() -> None:
    spec = engines.get(EngineId.AUTOSUBSYNC)
    assert spec.input_suffixes == frozenset({".srt"})
    assert not spec.accepts_subtitle(".ass")
