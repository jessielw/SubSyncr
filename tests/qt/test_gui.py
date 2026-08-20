"""The Qt layer, focused on the seam between it and the core.

Anything testable without Qt belongs in tests/core; these cover the parts that
only exist because there is a UI - table rendering, the review dialog, and the
cross-thread bridge.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
import threading

from PySide6.QtCore import QSettings, Qt
import pytest

from ffsubsyncui.core.models import JobStatus, SyncJob, SyncResult
from ffsubsyncui.core.queue import SyncQueue
from ffsubsyncui.qt.main_window import MainWindow
from ffsubsyncui.qt.match_view import BatchMatchDialog
from ffsubsyncui.qt.queue_view import (
    COLUMN_LANGUAGE,
    COLUMN_PROGRESS,
    COLUMN_RESULT,
    COLUMN_RUN,
    COLUMN_STATUS,
    COLUMN_SUBTITLE,
    JobTableModel,
)
from ffsubsyncui.qt.settings_view import SettingsDialog


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    instance = MainWindow(settings=QSettings("FFSubSyncUI-tests", "gui"))
    qtbot.addWidget(instance)
    yield instance
    instance.bridge.detach()
    instance.queue.shutdown()
    instance.store.close()


def test_window_starts_empty(window: MainWindow) -> None:
    assert window.model.rowCount() == 0
    assert "0 ready" in window.queue_label.text()


def test_queued_jobs_appear_in_the_table(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    window.queue.add([make_job("Show.S01E01"), make_job("Show.S01E02")])
    assert window.model.rowCount() == 2
    assert "2 ready" in window.queue_label.text()


def test_table_renders_job_detail(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("Show.S01E01")
    job.language = "en"
    window.queue.add([job])
    model = window.model
    assert model.data(model.index(0, COLUMN_STATUS)) == "Queued"
    assert model.data(model.index(0, COLUMN_SUBTITLE)) == "Show.S01E01.en.srt"
    assert model.data(model.index(0, COLUMN_LANGUAGE)) == "en"


def test_unchecking_run_disables_the_job(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("Show.S01E01")
    window.queue.add([job])
    model = window.model
    assert model.setData(
        model.index(0, COLUMN_RUN),
        Qt.CheckState.Unchecked,
        Qt.ItemDataRole.CheckStateRole,
    )
    assert window.queue.find(job.id).enabled is False  # type: ignore[union-attr]
    assert "0 ready" in window.queue_label.text()


def test_finished_job_shows_offset_and_duration(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("Show.S01E01")
    window.queue.add([job])
    job.status = JobStatus.SUCCEEDED
    job.result = SyncResult(
        status=JobStatus.SUCCEEDED, offset_seconds=-2.5, duration_seconds=3.25
    )
    model = window.model
    assert "offset -2.500s" in str(model.data(model.index(0, COLUMN_RESULT)))
    # Status already names the outcome, so this column reports the cost instead.
    assert model.data(model.index(0, COLUMN_PROGRESS)) == "3.2s"


def test_low_quality_result_is_not_shown_as_success(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    job = make_job("Show.S01E01")
    window.queue.add([job])
    job.status = JobStatus.SKIPPED_LOW_QUALITY
    job.result = SyncResult(
        status=JobStatus.SKIPPED_LOW_QUALITY, quality_reasons=["score 1.0 < 5.0"]
    )
    model = window.model
    assert model.data(model.index(0, COLUMN_STATUS)) == "Low quality"
    assert "unmodified" in str(model.data(model.index(0, COLUMN_RESULT)))


def test_actions_track_selection_and_state(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    assert window.start_action.isEnabled() is False
    window.queue.add([make_job("Show.S01E01")])
    assert window.start_action.isEnabled() is True
    assert window.stop_action.isEnabled() is False
    assert window.remove_action.isEnabled() is False
    window.table.selectRow(0)
    assert window.remove_action.isEnabled() is True


def test_bridge_delivers_worker_thread_events_to_the_gui(
    window: MainWindow, make_job: Callable[..., SyncJob], qtbot
) -> None:
    """The one thing the bridge exists for: cross-thread signal marshalling."""
    job = make_job("Show.S01E01")
    window.queue.add([job])
    received: list[str] = []
    window.bridge.log.connect(lambda jid, lvl, msg: received.append(msg))

    def emit_from_worker() -> None:
        callbacks = window.queue.callbacks
        assert callbacks.log is not None
        callbacks.log(job.id, "INFO", "from another thread")

    with qtbot.waitSignal(window.bridge.log, timeout=3000):
        threading.Thread(target=emit_from_worker).start()
    assert received == ["from another thread"]
    assert "from another thread" in window.log.toPlainText()


def test_detach_stops_forwarding(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    """A closing window must not be signalled by a still-draining queue."""
    window.bridge.detach()
    assert window.queue.callbacks.log is None


def test_match_dialog_builds_grouped_jobs(
    qtbot, media_tree: Callable[..., Path], window: MainWindow
) -> None:
    folder = media_tree(
        [
            "Show.S01E01.mkv",
            "Show.S01E01.en.srt",
            "Show.S01E01.es.srt",
            "Show.S01E02.mkv",
            "Show.S01E02.en.srt",
        ]
    )
    dialog = BatchMatchDialog(window.config.sync, parent=window)
    qtbot.addWidget(dialog)
    dialog.video_folder.set_text(str(folder))
    dialog._scan()

    assert dialog.table.rowCount() == 3
    jobs = dialog.jobs()
    assert len(jobs) == 3
    # Two videos, so two speech-cache groups.
    assert len({job.group_key for job in jobs}) == 2


def test_match_dialog_excludes_deselected_rows(
    qtbot, media_tree: Callable[..., Path], window: MainWindow
) -> None:
    folder = media_tree(["Show.S01E01.mkv", "Show.S01E01.en.srt"])
    dialog = BatchMatchDialog(window.config.sync, parent=window)
    qtbot.addWidget(dialog)
    dialog.video_folder.set_text(str(folder))
    dialog._scan()
    assert len(dialog.jobs()) == 1
    dialog._set_include_all(False)
    assert dialog.jobs() == []


def test_match_dialog_cannot_queue_an_unmatched_video(
    qtbot, media_tree: Callable[..., Path], window: MainWindow
) -> None:
    folder = media_tree(["Show.S01E01.mkv"])
    dialog = BatchMatchDialog(window.config.sync, parent=window)
    qtbot.addWidget(dialog)
    dialog.video_folder.set_text(str(folder))
    dialog._scan()
    assert dialog.jobs() == []
    assert dialog._ok_button.isEnabled() is False


def test_settings_round_trip(qtbot, window: MainWindow) -> None:
    dialog = SettingsDialog(window.config, parent=window)
    qtbot.addWidget(dialog)
    dialog.concurrency.setValue(4)
    dialog.vad.setCurrentText("auditok")
    dialog.split_penalty.toggle.setChecked(True)
    dialog.split_penalty.spin.setValue(6.0)
    dialog.output_suffix.setText("fixed")

    updated = dialog.config()
    assert updated.concurrency == 4
    assert updated.sync.vad == "auditok"
    assert updated.sync.split_penalty == 6.0
    # normalized() restores the leading dot.
    assert updated.output_suffix == ".fixed"


def test_settings_optional_values_stay_unset(qtbot, window: MainWindow) -> None:
    dialog = SettingsDialog(window.config, parent=window)
    qtbot.addWidget(dialog)
    assert dialog.config().sync.split_penalty is None
    assert dialog.config().sync.max_duration_seconds is None


def test_model_refresh_tracks_removals(
    window: MainWindow, make_job: Callable[..., SyncJob]
) -> None:
    jobs = [make_job("A"), make_job("B")]
    window.queue.add(jobs)
    assert window.model.rowCount() == 2
    window.queue.remove([jobs[0].id])
    assert window.model.rowCount() == 1
    assert window.model.data(window.model.index(0, COLUMN_SUBTITLE)) == "B.en.srt"


def test_touch_on_a_missing_job_is_harmless(window: MainWindow) -> None:
    model: JobTableModel = window.model
    model.touch("no such job")


def test_queue_is_a_real_sync_queue(window: MainWindow) -> None:
    assert isinstance(window.queue, SyncQueue)


def test_auto_start_runs_the_queue_on_add(
    window: MainWindow, make_job: Callable[..., SyncJob], monkeypatch
) -> None:
    """Queueing work should be one action, not two."""
    started: list[bool] = []
    monkeypatch.setattr(window.queue, "start", lambda: started.append(True))
    window.config.auto_start = True
    window._queue_jobs([make_job("Show.S01E01")], "Queued")
    assert started == [True]


def test_auto_start_off_leaves_the_queue_idle(
    window: MainWindow, make_job: Callable[..., SyncJob], monkeypatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(window.queue, "start", lambda: started.append(True))
    window.config.auto_start = False
    window._queue_jobs([make_job("Show.S01E01")], "Queued")
    assert started == []
    assert window.model.rowCount() == 1


def test_status_bar_reports_group_count_on_add(
    window: MainWindow, make_job: Callable[..., SyncJob], monkeypatch
) -> None:
    monkeypatch.setattr(window.queue, "start", lambda: None)
    window.config.auto_start = False
    window._queue_jobs([make_job("A"), make_job("B")], "Queued")
    assert "2 jobs across 2 videos" in window.statusBar().currentMessage()


def test_no_estimate_before_enough_samples(window: MainWindow) -> None:
    """An ETA from one data point is noise, so none is shown."""
    from ffsubsyncui.core.queue import QueueCounts

    window._batch_started = None
    assert window._estimate_remaining(QueueCounts(ready=5)) == ""


def test_fast_mode_applies_to_the_batch_only(
    qtbot, media_tree: Callable[..., Path], window: MainWindow
) -> None:
    """A per-batch choice must not quietly rewrite the saved settings."""
    folder = media_tree(["Show.S01E01.mkv", "Show.S01E01.en.srt"])
    dialog = BatchMatchDialog(window.config.sync, parent=window)
    qtbot.addWidget(dialog)
    dialog.video_folder.set_text(str(folder))
    dialog._scan()

    assert dialog.jobs()[0].options.multi_segment_sync is False
    dialog.fast_mode.setChecked(True)
    assert dialog.jobs()[0].options.multi_segment_sync is True
    # The shared settings object is untouched.
    assert window.config.sync.multi_segment_sync is False
