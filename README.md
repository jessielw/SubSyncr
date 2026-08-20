# FFSubSync UI

FFSubSync UI is a cross-platform PySide6 desktop queue for
[ffsubsync](https://github.com/smacke/ffsubsync). It supports explicit sync jobs
and review-first batch matching for folders of similarly named media and subtitle
files.

## Current features

- Sequential, non-blocking job queue with per-job progress and results.
- Enabled checkboxes to leave jobs queued for later.
- Persistent unfinished queue with safe crash recovery.
- Regex-based episode/date matching followed by RapidFuzz similarity matching.
- Manual review and correction of every batch pairing.
- Safe sibling outputs such as `episode.en.synced.srt` by default.
- System `ffmpeg`/`ffprobe` detection or a custom path in Settings.
- Local PyInstaller onedir builds for Windows, macOS, and Linux.

The active ffsubsync operation cannot currently be cancelled cooperatively.
`Stop after current` therefore finishes the active file safely and prevents the
next enabled job from starting.

## Requirements

- Python 3.12 or 3.13
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on PATH, or their directory configured in Settings

## Run from source

```powershell
uv sync --group dev
uv run ffsubsyncui
```

On macOS or Linux, the same commands work in a POSIX shell.

## Development checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest
```

Tests do not require ffmpeg. A real end-to-end sync can be exercised manually by
adding a reference media file and subtitle in the application.

## Build a local application bundle

PyInstaller cannot cross-compile: run the build on every operating system for
which you want an artifact.

```powershell
uv run --group build python build.py
```

The onedir bundle is written to `dist/FFSubSyncUI`. FFmpeg is deliberately not
bundled; users can install it system-wide or configure a custom location.

## Queue storage

Settings use Qt's native settings backend. Unfinished jobs are stored as
versioned JSON in Qt's per-user application-data location. Successful jobs are
kept for the current session but are not restored as history on the next launch.

