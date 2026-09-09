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

from subsyncr.core.config import Theme
from subsyncr.core.models import JobStatus, SyncJob, SyncResult
from subsyncr.core.queue import SyncQueue
from subsyncr.qt.add_job import AddJobDialog
from subsyncr.qt.main_window import MainWindow
from subsyncr.qt.match_view import BatchMatchDialog
from subsyncr.qt.queue_view import (
    COLUMN_LANGUAGE,
    COLUMN_PROGRESS,
    COLUMN_RESULT,
    COLUMN_RUN,
    COLUMN_STATUS,
    COLUMN_SUBTITLE,
    JobTableModel,
)
from subsyncr.qt.settings_view import SettingsDialog


@pytest.fixture
def window(qtbot) -> Iterator[MainWindow]:
    instance = MainWindow(settings=QSettings("SubSyncr-tests", "gui"))
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
    assert updated.sync.ffsubsync.vad == "auditok"
    assert updated.sync.ffsubsync.split_penalty == 6.0
    # normalized() restores the leading dot.
    assert updated.output_suffix == ".fixed"


def test_settings_optional_values_stay_unset(qtbot, window: MainWindow) -> None:
    dialog = SettingsDialog(window.config, parent=window)
    qtbot.addWidget(dialog)
    assert dialog.config().sync.ffsubsync.split_penalty is None
    assert dialog.config().sync.ffsubsync.max_duration_seconds is None


def test_settings_theme_defaults_to_automatic(qtbot, window: MainWindow) -> None:
    dialog = SettingsDialog(window.config, parent=window)
    qtbot.addWidget(dialog)
    assert dialog.config().theme is Theme.AUTOMATIC


def test_settings_theme_choice_round_trips(qtbot, window: MainWindow) -> None:
    dialog = SettingsDialog(window.config, parent=window)
    qtbot.addWidget(dialog)
    dialog.theme.setCurrentIndex(dialog.theme.findData(Theme.DARK))
    assert dialog.config().theme is Theme.DARK


def test_cancelling_settings_reverts_the_previewed_theme(
    qtbot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The offscreen QPA platform used for tests doesn't actually report color
    # scheme changes, so spy on apply_theme rather than styleHints().
    applied: list[Theme] = []
    monkeypatch.setattr("subsyncr.qt.settings_view.apply_theme", applied.append)

    dialog = SettingsDialog(window.config, parent=window)
    qtbot.addWidget(dialog)

    dialog.theme.setCurrentIndex(dialog.theme.findData(Theme.DARK))
    assert applied == [Theme.DARK]

    dialog.reject()
    assert applied == [Theme.DARK, Theme.AUTOMATIC]


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
    from subsyncr.core.queue import QueueCounts

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

    assert dialog.jobs()[0].options.ffsubsync.multi_segment_sync is False
    dialog.fast_mode.setChecked(True)
    assert dialog.jobs()[0].options.ffsubsync.multi_segment_sync is True
    # The shared settings object is untouched.
    assert window.config.sync.ffsubsync.multi_segment_sync is False


def test_dropping_files_opens_a_prefilled_review(
    qtbot, media_tree: Callable[..., Path], window: MainWindow, monkeypatch
) -> None:
    """Dropped files should land in the review grid, not a folder scan."""
    folder = media_tree(["Show.S01E01.mkv", "Show.S01E01.en.srt", "Show.S01E01.es.srt"])
    captured: list[BatchMatchDialog] = []
    monkeypatch.setattr(
        MainWindow, "_run_match_dialog", lambda self, dialog: captured.append(dialog)
    )
    window._match_dropped_files(sorted(folder.iterdir()))

    assert captured, "a review dialog should have been built"
    dialog = captured[0]
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 2
    assert dialog.subtitle_reference.isChecked() is False
    assert {job.reference_path.name for job in dialog.jobs()} == {"Show.S01E01.mkv"}


def test_dropping_subtitles_without_a_video_explains_itself(
    media_tree: Callable[..., Path], window: MainWindow, monkeypatch
) -> None:
    """Which of several subtitles is the reference is not knowable."""
    folder = media_tree(["Show.S01E01.en.srt", "Show.S01E01.es.srt"])
    shown: list[str] = []
    monkeypatch.setattr(
        "subsyncr.qt.main_window.QMessageBox.information",
        lambda *args, **kwargs: shown.append(args[2]),
    )
    monkeypatch.setattr(
        MainWindow, "_run_match_dialog", lambda self, dialog: pytest.fail("no dialog")
    )
    window._match_dropped_files(sorted(folder.iterdir()))
    assert shown and "video" in shown[0]


def test_dropping_a_single_video_opens_add_job_prefilled(
    qtbot, media_tree: Callable[..., Path], window: MainWindow, monkeypatch
) -> None:
    """One video and nothing else is exactly the single-job case."""
    folder = media_tree(["Show.S01E01.mkv"])
    captured: list[AddJobDialog] = []
    monkeypatch.setattr(
        MainWindow, "_run_add_job_dialog", lambda self, dialog: captured.append(dialog)
    )
    window._match_dropped_files(sorted(folder.iterdir()))

    assert captured, "an Add job dialog should have been built"
    dialog = captured[0]
    qtbot.addWidget(dialog)
    assert dialog.reference.text() == str(folder / "Show.S01E01.mkv")
    assert dialog.subtitle.text() == ""


def test_dropping_a_single_subtitle_opens_add_job_prefilled(
    qtbot, media_tree: Callable[..., Path], window: MainWindow, monkeypatch
) -> None:
    """One subtitle and nothing else is exactly the single-job case."""
    folder = media_tree(["Show.S01E01.en.srt"])
    captured: list[AddJobDialog] = []
    monkeypatch.setattr(
        MainWindow, "_run_add_job_dialog", lambda self, dialog: captured.append(dialog)
    )
    window._match_dropped_files(sorted(folder.iterdir()))

    assert captured, "an Add job dialog should have been built"
    dialog = captured[0]
    qtbot.addWidget(dialog)
    assert dialog.subtitle.text() == str(folder / "Show.S01E01.en.srt")
    assert dialog.reference.text() == ""


def test_dropping_multiple_videos_without_subtitles_opens_an_unmatched_review(
    qtbot, media_tree: Callable[..., Path], window: MainWindow, monkeypatch
) -> None:
    """No subtitles at all should not be a dead end for a multi-video drop."""
    folder = media_tree(["Show.S01E01.mkv", "Show.S01E02.mkv"])
    captured: list[BatchMatchDialog] = []
    monkeypatch.setattr(
        MainWindow, "_run_match_dialog", lambda self, dialog: captured.append(dialog)
    )
    window._match_dropped_files(sorted(folder.iterdir()))

    assert captured, "a review dialog should have been built"
    dialog = captured[0]
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 2
    assert dialog.jobs() == []


def test_subtitle_reference_mode_disables_fast_mode(qtbot, window: MainWindow) -> None:
    """Sampling audio makes no sense when the reference is a text file."""
    dialog = BatchMatchDialog(window.config.sync, parent=window)
    qtbot.addWidget(dialog)
    assert dialog.fast_mode.isEnabled() is True
    dialog.subtitle_reference.setChecked(True)
    assert dialog.fast_mode.isEnabled() is False


def test_backup_checkbox_follows_overwrite(qtbot, window: MainWindow) -> None:
    dialog = AddJobDialog(window.config.sync, parent=window)
    qtbot.addWidget(dialog)
    assert dialog.backup.isEnabled() is False
    dialog.overwrite.setChecked(True)
    assert dialog.backup.isEnabled() is True
    assert dialog.backup.isChecked() is True
