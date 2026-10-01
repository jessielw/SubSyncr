"""Failure and race cases for publishing user files."""

from pathlib import Path
from unittest.mock import patch

import pytest

from subsyncr.core.models import FileOptions, OutputPolicy
from subsyncr.core.output import OutputTransaction, backup_original


def test_a_destination_created_during_alignment_is_not_replaced(tmp_path: Path):
    source = tmp_path / "input.srt"
    target = tmp_path / "output.srt"
    source.write_bytes(b"original")
    with OutputTransaction(source, target, FileOptions()) as transaction:
        transaction.staging.write_bytes(b"aligned")
        target.write_bytes(b"created by another application")
        with pytest.raises(FileExistsError):
            transaction.commit()
    assert target.read_bytes() == b"created by another application"


def test_partial_backup_is_never_published(tmp_path: Path):
    source = tmp_path / "input.srt"
    source.write_bytes(b"original")

    def fail_copy(source, staged):
        staged.write_bytes(b"partial")
        raise OSError("disk full")

    with (
        patch("subsyncr.core.output.shutil.copy2", side_effect=fail_copy),
        pytest.raises(OSError, match="disk full"),
    ):
        backup_original(source)
    assert not source.with_suffix(".srt.bak").exists()
    assert source.read_bytes() == b"original"


def test_failed_backup_prevents_opening_transaction(tmp_path: Path):
    source = tmp_path / "input.srt"
    source.write_bytes(b"original")
    files = FileOptions(output_policy=OutputPolicy.OVERWRITE)
    with (
        patch("subsyncr.core.output.backup_original", side_effect=OSError("denied")),
        pytest.raises(OSError, match="original left untouched"),
        OutputTransaction(source, source, files),
    ):
        pytest.fail("alignment must not start")
    assert source.read_bytes() == b"original"


def test_staging_paths_are_unique_and_failed_output_is_discarded(tmp_path: Path):
    source = tmp_path / "input.srt"
    target = tmp_path / "output.srt"
    source.write_bytes(b"original")
    with (
        OutputTransaction(source, target, FileOptions()) as first,
        OutputTransaction(source, target, FileOptions()) as second,
    ):
        assert first.staging != second.staging
        first.staging.write_bytes(b"partial")
    assert not target.exists()
    assert not list(tmp_path.glob(".subsyncr-output-*"))


def test_native_engine_cancellation_never_promotes_output(tmp_path: Path):
    import io
    import json

    from subsyncr.core.engine import build_spec
    from subsyncr.core.engines import EngineId
    from subsyncr.core.engines.runner import Outcome, run_each
    from subsyncr.core.models import SyncJob
    from subsyncr.core.worker import CancelToken, EventChannel

    source = tmp_path / "input.srt"
    target = tmp_path / "output.srt"
    source.write_bytes(b"original")
    target.write_bytes(b"previous output")
    job = SyncJob(
        reference_path=tmp_path / "ref.srt", subtitle_path=source, output_path=target
    )
    job.options.files.replace_output = True
    cancel = CancelToken()
    stream = io.StringIO()

    def align(reference, subtitle, output, options, job_id):
        output.write_bytes(b"aligned")
        cancel.cancel()
        return Outcome("succeeded")

    run_each(
        build_spec([job]),
        EventChannel(stream),
        cancel,
        engine=EngineId.ALASS,
        run_one=align,
    )
    assert target.read_bytes() == b"previous output"
    results = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert next(e for e in results if e["event"] == "result")["status"] == "cancelled"
