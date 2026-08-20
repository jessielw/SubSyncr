# FFSubSync UI

A desktop batch queue for [ffsubsync](https://github.com/smacke/ffsubsync), built
for the job it is actually good at: taking a folder of video files and a folder of
loosely-named subtitle files, working out which belongs to which, letting you check
that before anything runs, and then syncing the lot.

Bazarr already handles subtitle sync for a Sonarr/Radarr-managed library, and does
it well. It cannot help before a library exists — with a pile of loose files whose
names do not line up and no episode records behind them. That gap is what this
fills, so the interesting part here is the **matching and review**, not the syncing.

## What it does

- **Review-first batch matching.** Episode identifiers, then exact normalized
  names, then RapidFuzz similarity. Every proposal shows its method, its
  confidence, and why — including a warning when the runner-up was close.
- **One video, many subtitles.** `ep.en.srt`, `ep.es.srt` and `ep.en.forced.srt`
  all pair with the same video instead of competing for it, and all sync from a
  single audio extraction.
- **A real queue.** Parallel by default, per-job cancellation, retry,
  crash-recoverable pending work, and searchable history with offsets and scores.
- **The full ffsubsync option surface** — VAD backend, piecewise `--split-penalty`
  alignment, segment sampling, reference stream selection, and the quality gate.
- **Speech reuse between sessions**, so a language added later skips extraction.
- **A headless CLI** over the same core, for scripted batches.

## Requirements

- Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on `PATH`, or their folder set in Settings.
  Neither is needed when the reference is itself a subtitle file.

## Run

```powershell
uv sync --group dev
uv run ffsubsyncui            # GUI
uv run ffsubsyncui doctor     # report paths, ffmpeg status and versions
```

### CLI

```powershell
uv run ffsubsyncui match  D:\media\Season01              # propose pairings only
uv run ffsubsyncui match  D:\media\Season01 --json
uv run ffsubsyncui batch  D:\media\Season01 --auto-match 90 --workers 2
uv run ffsubsyncui sync   episode.mkv episode.en.srt
uv run ffsubsyncui history --limit 20
```

`match` and `batch` take an optional second folder when the subtitles live
somewhere other than beside the video files.

## Architecture

```
core/   scanner, matcher, queue, subprocess engine, store, config   (no Qt)
cli/    headless frontend over core                                 (no Qt)
qt/     PySide6 frontend; bridge.py is the only thread seam
```

Nothing under `core/` or `cli/` may import PySide6, and `tests/test_boundary.py`
enforces that rather than trusting it. The CLI exists partly to keep the boundary
honest: anything it cannot reach is logic that leaked into the GUI.

### Why syncs run in their own process

`ffsubsync` calls `logging.basicConfig()` at import and reconfigures the root
logger, and offers no cancellation hook. A subprocess fixes both, makes the
alignment score — which ffsubsync only logs and never returns — cleanly
recoverable, and returns every byte the sync allocated the moment it exits
(measured: ~104 MB peak, 0 MB after).

The child speaks newline-delimited JSON on stdout and takes exclusive ownership of
that descriptor, so nothing a dependency prints can corrupt the channel.

### Why the unit of work is a video, not a subtitle

Extracting speech from the reference dominates the cost; aligning a parsed
subtitle against an already-extracted reference is almost free. So one worker
process handles a whole *group* — one video and every subtitle queued against it
— building the reference pipeline once with `make_reference_pipe` and reusing it
across `try_sync` calls.

Measured on 24 jobs across 8 videos, cold cache:

| | 1 worker | 2 workers | 4 workers |
|---|---|---|---|
| one process per subtitle | 11.95 s | 7.03 s | 4.30 s |
| one process per video | **5.79 s** | **3.04 s** | **1.66 s** |

Two effects compound: 8 process startups instead of 24 (~0.37 s each from source,
more when frozen), and 8 audio extractions instead of 24. A sibling subtitle costs
about 0.03 s instead of 0.45 s.

This also removes a scheduling problem rather than solving it. When siblings were
separate jobs, one had to build the shared speech array while the others either
stalled or duplicated the work. Owning the group makes that race impossible.

The on-disk speech cache still earns its place, but only *across* runs: sync
English today and Spanish next week and the second run skips the audio pass. The
arrays are tiny — 8 videos came to 12 KB.

Concurrency defaults to half the logical cores, capped at four. Raise or lower it
in Settings; on a spinning disk fewer parallel reads may well be faster.

### Two ffsubsync results that look like success

- `retval` stays `0` even when an alignment fails. Success needs `retval == 0`
  **and** `sync_was_successful` **and** the output file existing.
- `--skip-sync-on-low-quality` writes the *original* subtitles to the output path.
  A file being there proves nothing; those jobs report **Low quality**, not Done.

## Development

```powershell
uv run pytest
uv run pytest -m "not slow"          # skip tests that spawn real ffsubsync
uv run ruff check . ; uv run ruff format --check . ; uv run basedpyright
```

Tests need neither ffmpeg nor a display: the integration tests use subtitle files
as the reference, and the Qt tests run offscreen. Set `FFSUBSYNCUI_DATA_DIR` to
keep config, database and cache out of the real user data directory.

## Build

PyInstaller cannot cross-compile, so run this on each target OS:

```powershell
uv run --group build python build.py
```

The onedir bundle lands in `dist/FFSubSyncUI`. The executable wears three hats and
dispatches on its first argument: `--sync-worker` for the child process, a
subcommand for the CLI, and anything else for the GUI. ffmpeg is deliberately not
bundled.

## Where things are stored

`ffsubsyncui doctor` prints the exact paths. Config is JSON, the queue and history
are SQLite, and cached speech arrays live under the platform cache directory.
