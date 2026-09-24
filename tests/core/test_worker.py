"""The worker protocol: spec framing, engine dispatch, and cancellation.

These are about the contract between parent and child, not about any engine.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from subsyncr.core import embedded
from subsyncr.core.embedded import Track
from subsyncr.core.worker import (
    CancelToken,
    EventChannel,
    dispatch,
    use_pgs_reference,
)


class _Sink(io.StringIO):
    """A stdout stand-in that records the events written to it."""

    def events(self) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in self.getvalue().splitlines() if line.strip()
        ]


def test_events_are_one_json_object_per_line() -> None:
    sink = _Sink()
    channel = EventChannel(sink)
    channel.emit("progress", job_id="a", fraction=0.5)
    channel.emit("result", job_id="a", status="succeeded")
    assert [event["event"] for event in sink.events()] == ["progress", "result"]
    assert sink.events()[0]["fraction"] == 0.5


def test_emitting_survives_a_closed_parent() -> None:
    """A cancelled parent closes the pipe mid-run; that is not a crash."""
    sink = _Sink()
    channel = EventChannel(sink)
    sink.close()
    channel.emit("log", message="into the void")  # Must not raise.


def test_concurrent_emits_never_interleave() -> None:
    """Engines report from a reader thread while progress comes from another."""
    sink = _Sink()
    channel = EventChannel(sink)
    threads = [
        threading.Thread(target=channel.emit, args=("log",), kwargs={"n": index})
        for index in range(50)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(event["n"] for event in sink.events()) == list(range(50))


def test_dispatch_rejects_an_unknown_engine() -> None:
    sink = _Sink()
    code = dispatch({"engine": "wishful"}, EventChannel(sink), CancelToken())
    assert code == 2
    assert sink.events()[-1]["event"] == "fatal"
    assert "wishful" in sink.events()[-1]["error"]


def test_dispatch_defaults_to_ffsubsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec from an older queue row has no engine field."""
    seen: dict[str, Any] = {}

    def fake_run_group(spec: dict[str, Any], channel: Any, cancel: Any) -> int:
        seen["spec"] = spec
        return 0

    module = sys.modules["subsyncr.core.engines.ffsubsync"] = type(sys)("stub")
    monkeypatch.setattr(module, "run_group", fake_run_group, raising=False)
    try:
        assert dispatch({"items": []}, EventChannel(_Sink()), CancelToken()) == 0
        assert seen["spec"] == {"items": []}
    finally:
        del sys.modules["subsyncr.core.engines.ffsubsync"]


# -- PGS reference --------------------------------------------------------


def _pgs_spec(stream: str, reference: str = "ep.mkv") -> dict[str, Any]:
    return {
        "engine": "ffsubsync",
        "reference": reference,
        "options": {"ffsubsync": {"pgs_ref_stream": stream}},
        "items": [{"id": "job-1", "language": "en"}],
    }


def _probe_returns(monkeypatch: pytest.MonkeyPatch, tracks: list[Track]) -> None:
    monkeypatch.setattr(embedded, "probe_tracks", lambda *_: tracks)


def test_auto_pgs_resolves_to_a_track(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe_returns(
        monkeypatch,
        [
            Track(index=0, codec="hdmv_pgs_subtitle", language="fre"),
            Track(index=1, codec="hdmv_pgs_subtitle", language="eng"),
        ],
    )
    spec = _pgs_spec("auto")
    use_pgs_reference(spec, EventChannel(_Sink()))
    assert spec["options"]["ffsubsync"]["pgs_ref_stream"] == "s:1"


def test_auto_pgs_without_a_track_falls_back_to_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffsubsync would otherwise fail every job in the group."""
    _probe_returns(monkeypatch, [Track(index=0, codec="subrip")])
    spec = _pgs_spec("auto")
    sink = _Sink()
    use_pgs_reference(spec, EventChannel(sink))
    assert spec["options"]["ffsubsync"]["pgs_ref_stream"] == ""
    assert "using the audio" in sink.events()[-1]["message"]


def test_explicit_pgs_stream_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe_returns(monkeypatch, [])
    spec = _pgs_spec("s:3")
    use_pgs_reference(spec, EventChannel(_Sink()))
    assert spec["options"]["ffsubsync"]["pgs_ref_stream"] == "s:3"


def test_pgs_is_not_probed_for_a_subtitle_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> list[Track]:
        raise AssertionError("probed a subtitle reference")

    monkeypatch.setattr(embedded, "probe_tracks", fail)
    spec = _pgs_spec("auto", reference="extracted.srt")
    use_pgs_reference(spec, EventChannel(_Sink()))
    assert spec["options"]["ffsubsync"]["pgs_ref_stream"] == "auto"


# -- cancellation --------------------------------------------------------


def test_a_fresh_token_is_not_cancelled() -> None:
    assert not CancelToken().cancelled


def test_cancel_terminates_an_attached_child() -> None:
    token = CancelToken()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    token.attach(child)
    try:
        token.cancel()
        assert child.wait(timeout=10) is not None
        assert token.cancelled
    finally:
        if child.poll() is None:  # pragma: no cover - only on a failed cancel
            child.kill()


def test_attaching_after_a_cancel_still_reaps_the_child() -> None:
    """The race that would otherwise leak a process for the full sync."""
    token = CancelToken()
    token.cancel()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        token.attach(child)
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:  # pragma: no cover - only on a failed cancel
            child.kill()


def test_cancel_is_safe_with_nothing_attached() -> None:
    token = CancelToken()
    token.cancel()
    token.cancel()
    token.detach()
    assert token.cancelled


# -- end to end ----------------------------------------------------------


def _run_worker(stdin_payload: str, *, close_stdin: bool = True) -> tuple[int, str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "subsyncr.core.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None
    process.stdin.write(stdin_payload)
    process.stdin.flush()
    if close_stdin:
        process.stdin.close()
    output, _ = process.communicate(timeout=60)
    return process.returncode, output


def test_a_malformed_spec_is_reported_not_crashed() -> None:
    code, output = _run_worker("not json at all\n")
    assert code == 2
    assert '"event": "fatal"' in output


def test_closing_stdin_without_a_spec_exits_promptly() -> None:
    started = time.monotonic()
    code, output = _run_worker("")
    assert code == 2
    assert "No job spec" in output
    assert time.monotonic() - started < 30


_INHERITS_STDIN = """
import subprocess, sys
from subsyncr.core.worker import claim_stdin
control = claim_stdin()
# Anything an engine spawns inherits fd 0. If that is still the parent's pipe,
# a tool that reads stdin (ffmpeg does) blocks until the sync is cancelled.
child = subprocess.run(
    [sys.executable, "-c", "import sys; sys.stdin.read()"], timeout=20
)
print("child-exit", child.returncode, file=sys.stderr, flush=True)
print("control", control.readline().strip(), file=sys.stderr, flush=True)
"""


def test_children_inherit_an_empty_stdin_not_the_control_pipe() -> None:
    """The parent holds stdin open to signal cancel; children must not see it."""
    process = subprocess.Popen(
        [sys.executable, "-c", _INHERITS_STDIN],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None
    process.stdin.write("still-open\n")
    process.stdin.flush()
    try:
        # The pipe is deliberately left open, exactly as a running sync would.
        _, errors = process.communicate(timeout=60)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - the bug guarded
        process.kill()
        raise AssertionError("a spawned child inherited the open control pipe") from exc
    assert "child-exit 0" in errors
    # ...and the control channel itself still carries the parent's messages.
    assert "control still-open" in errors
