from __future__ import annotations

from typing import cast

from PySide6.QtCore import QSettings

from ffsubsyncui.domain import SyncOptions


def load_default_options(settings: QSettings) -> SyncOptions:
    settings.beginGroup("sync")
    try:
        return SyncOptions(
            encoding=str(settings.value("encoding", "infer")),
            output_encoding=str(settings.value("output_encoding", "utf-8")),
            vad=str(settings.value("vad", "subs_then_webrtc")),
            max_offset_seconds=float(str(settings.value("max_offset_seconds", 60.0))),
            no_fix_framerate=cast(
                bool, settings.value("no_fix_framerate", False, type=bool)
            ),
            quality_gate=cast(bool, settings.value("quality_gate", False, type=bool)),
            reference_stream=str(settings.value("reference_stream", "")),
            ffmpeg_path=str(settings.value("ffmpeg_path", "")),
        )
    finally:
        settings.endGroup()


def save_default_options(settings: QSettings, options: SyncOptions) -> None:
    settings.beginGroup("sync")
    try:
        settings.setValue("encoding", options.encoding)
        settings.setValue("output_encoding", options.output_encoding)
        settings.setValue("vad", options.vad)
        settings.setValue("max_offset_seconds", options.max_offset_seconds)
        settings.setValue("no_fix_framerate", options.no_fix_framerate)
        settings.setValue("quality_gate", options.quality_gate)
        settings.setValue("reference_stream", options.reference_stream)
        settings.setValue("ffmpeg_path", options.ffmpeg_path)
    finally:
        settings.endGroup()
