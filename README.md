# SubSyncr

A desktop batch queue for subtitle synchronization, driving four engines -
[ffsubsync](https://github.com/smacke/ffsubsync),
[alass](https://github.com/kaegi/alass),
[lapse](https://github.com/Schwponaco-org/lapse) and
[autosubsync](https://github.com/oseiskar/autosubsync) - and built for the job
they are actually good at: taking a folder of video files and a folder of
loosely-named subtitle files, working out which belongs to which, letting you check
that before anything runs, and then syncing the lot.

Bazarr already handles subtitle sync for a Sonarr/Radarr-managed library, and does
it well. It cannot help before a library exists - with a pile of loose files whose
names do not line up and no episode records behind them. That gap is what this
fills, so the interesting part here is the **matching and review**, not the syncing.

## Install

1. Download the archive for your OS from
   [GitHub Releases](https://github.com/jessielw/SubSyncr/releases):
   Windows x64, Linux x64, macOS Apple Silicon (arm64) or macOS Intel (x64).
2. Unpack it anywhere and run `SubSyncr` (`SubSyncr.exe` on Windows,
   `SubSyncr.app` on macOS). Nothing is installed system-wide.
3. For an audio reference you also need `ffmpeg` and `ffprobe`, which are not
   bundled. Get them from [ffmpeg.org](https://ffmpeg.org/download.html) (or
   `brew install ffmpeg` on macOS), then either put them on `PATH` or point
   Settings at their folder. Syncing against a subtitle file needs neither.

**macOS:** the app is ad-hoc signed, without Apple Developer ID signing or
notarization, so Gatekeeper can block the first launch. If you trust the download,
try to open it once, then allow it under System Settings → Privacy & Security →
**Open Anyway**. See [Apple's instructions](https://support.apple.com/en-us/102445).

## Your first sync

1. Open **Match folders**, choose your video and subtitle folders, and click **Scan**.
2. Review each proposed pair. Uncheck unwanted rows or double-click to correct a match.
3. Queue the selected pairs and click **Start queue**. By default, results appear
   beside their inputs as `.synced.srt` (or the original supported text format).
4. Check the result and play a sample before using **Overwrite**. If backup creation
   fails, the app refuses to overwrite. Failed and low-quality results keep existing
   files intact.

**Help > Copy diagnostics** copies versions and platform details for a bug report,
without media paths or job history. **Help > Report a problem** opens the issue form.

![Matching sample video and subtitle filenames](docs/images/match-light.png)

Example review using sample filenames. The app also has a dark theme:

![SubSyncr queue in dark mode](docs/images/queue-dark.png)

## What it does

- **Review-first batch matching.** Episode identifiers, then exact normalized
  names, then RapidFuzz similarity. Every proposal shows its method, its
  confidence, and why - including a warning when the runner-up was close.
- **One video, many subtitles.** `ep.en.srt`, `ep.es.srt` and `ep.en.forced.srt`
  all pair with the same video instead of competing for it, and all sync from a
  single audio extraction.
- **A real queue.** Parallel by default, per-job cancellation, retry,
  crash-recoverable pending work, one GUI per data folder, and searchable history with offsets and scores.
- **Four engines with a fallback chain.** Pick ffsubsync, alass, lapse or
  autosubsync per job, and set in Settings which engines to retry with, in
  order, when one fails or returns a low-quality verdict. alass and lapse are
  standalone binaries, downloaded on first use and verified against a pinned
  SHA-256 before they ever run.
- **Embedded references.** Use a text subtitle track already inside the video
  as the reference instead of its audio - comparing two lists of timestamps
  rather than decoding the film - or a PGS (Blu-ray image) track's display
  timings with ffsubsync, picked by language when set to Auto. When the files
  you drop need an engine other than the chosen one, the job says so and a
  capable engine from the chain runs instead.
- **The full ffsubsync option surface** - VAD backend, piecewise `--split-penalty`
  alignment, segment sampling, reference stream selection, and the quality gate.
- **Speech reuse between sessions**, so a language added later skips extraction.
- **Fast mode** for long references: sample segments instead of decoding in full.
- **Subtitle-to-subtitle alignment.** Align against a known-good subtitle
  instead of audio: no ffmpeg, and four episodes in well under a second.
- **Drag and drop.** Drop folders, or a full set of files, on the window to
  open a pre-filled review; drop a single video or a single subtitle and
  Add job opens with that file already filled in.
- **Light and dark, following the OS** or pinned in Settings, with icons and
  status colours that re-tint live rather than staying stuck at whatever the
  scheme was on launch.
- **Safe file publication.** Every engine writes to a temporary file first.
  Requested backups must succeed before an in-place overwrite; an existing `.bak`
  is never replaced. Conflicting queue destinations are rejected, and video-based
  names retain language/forced/SDH tags.
- **A log file** with its level and retention set in Settings, and an
  **Open log folder** button there and on error dialogs. Sync workers forward
  their log records to the main process, so one file covers the whole run.
- **A headless CLI** over the same core, for scripted batches.

## Platform notes

- alass publishes no macOS build, so SubSyncr cannot download it there; the app
  says so and suggests `brew install alass`. lapse covers Windows, Linux and
  macOS on both x64 and arm64.
- PGS references come from a
  [fork of ffsubsync](https://github.com/jessielw/ffsubsync) carrying a fix that
  is pending upstream
  ([smacke/ffsubsync#237](https://github.com/smacke/ffsubsync/pull/237)). The
  pin goes away once a release includes it; until then SubSyncr ships as
  release downloads only, not on PyPI.

## Run from source

Needs Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/), plus
`ffmpeg`/`ffprobe` as above.

```powershell
uv sync --group dev
uv run subsyncr            # GUI
uv run subsyncr doctor     # report paths, ffmpeg status and versions
```

### CLI

On Windows the release executable is a windowed GUI app. Use the source installation
below for terminal CLI commands. Linux and macOS executables support both modes.


```powershell
uv run subsyncr match  D:\media\Season01              # propose pairings only
uv run subsyncr match  D:\media\Season01 --json
uv run subsyncr batch  D:\media\Season01 --auto-match 90 --workers 2
uv run subsyncr batch  D:\movies --fast                  # sample, do not read in full
uv run subsyncr batch  D:\good-subs D:\bad-subs --subs-as-reference
uv run subsyncr sync   episode.mkv episode.en.srt
uv run subsyncr history --limit 20
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

Every engine runs in a worker process, whether it is a Python library loaded
inside the worker (ffsubsync, autosubsync) or an external binary the worker
launches (alass, lapse). The reason started with ffsubsync: it calls
`logging.basicConfig()` at import and reconfigures the root logger, and offers no
cancellation hook. A subprocess fixes both, makes the alignment score - which
ffsubsync only logs and never returns - cleanly recoverable, and returns every
byte the sync allocated the moment it exits (measured: ~104 MB peak, 0 MB after).
The same process boundary gives every engine one way to be cancelled and one
way to report.

The child speaks newline-delimited JSON on stdout and takes exclusive ownership of
that descriptor, so nothing a dependency prints can corrupt the channel.

### Why the unit of work is a video, not a subtitle

Extracting speech from the reference dominates the cost; aligning a parsed
subtitle against an already-extracted reference is almost free. So one worker
process handles a whole *group* - one video and every subtitle queued against it
- building the reference pipeline once with `make_reference_pipe` and reusing it
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
arrays are tiny - 8 videos came to 12 KB.

Concurrency defaults to half the logical cores, capped at four. Raise or lower it
in Settings; on a spinning disk fewer parallel reads may well be faster.

### Theming, without a stylesheet

Light/dark is Qt's own `QStyleHints.setColorScheme()` on top of the Fusion
style, so Qt repaints every standard widget itself and tracks the OS setting
live when the choice is *Automatic*.

No stylesheet is set anywhere, deliberately: under Fusion a QSS on a container
makes its children resolve colours from the stylesheet defaults rather than the
palette, which inverts enabled and disabled text. `tests/qt/test_widgets.py`
and `test_gui.py` assert that no widget carries one.

That leaves the colours Qt cannot know about. Icons are Phosphor glyphs from
qtawesome, rendered at a fixed colour, so `qt/theme.py`'s `ThemeManager` keeps a
weak registry of every icon owner and re-tints them from `QPalette.WindowText`
whenever the scheme changes - guarded with `shiboken6.isValid`, because a PySide
wrapper can outlive its C++ object. Semantic colours (`qt/palette.py`) come in
light/dark pairs and are resolved per paint. Qt's dark scheme also derives
`AlternateBase` from the Windows accent colour, which lands on a navy that reads
as a selected row, so row stripes are computed from `Base` instead.

### Fast mode

Fast mode is `--multi-segment-sync`: ffsubsync samples a handful of short
segments spread across the reference rather than decoding it end to end. On a
40-minute reference that measured 2.57 s to 1.40 s for the same answer
(-2.96 s vs -2.97 s). It is a per-batch toggle in the match dialog, and `--fast`
on the CLI, because it pays off on long films and saves little on a short episode.

Two neighbouring ffsubsync options are deliberately **not** wired up:

- `--max-duration-seconds` truncates the reference, and returned a confidently
  wrong `-11.67 s` where the correct offset was `-2.96 s`.
- `--skip-intro-outro` moved the answer to `+7.28 s` in testing. It only avoids
  sampling ~90 seconds of a multi-hour file, so the risk is not worth the saving.

### Two ffsubsync results that look like success

- `retval` stays `0` even when an alignment fails. Success needs `retval == 0`
  **and** `sync_was_successful` **and** the output file existing.
- `--skip-sync-on-low-quality` writes the *original* subtitles to its output path.
  SubSyncr keeps that output temporary and discards it; those jobs report
  **Low quality** and leave the final destination untouched.

## Development

```powershell
uv run pytest
uv run pytest -m "not slow"          # skip tests that spawn real ffsubsync
uv run ruff check .
uv run ruff format --check .
uv run --group build basedpyright
```

Tests need neither ffmpeg nor a display: the integration tests use subtitle files
as the reference, and the Qt tests run offscreen. Set `SUBSYNCR_DATA_DIR` to
keep config, database and cache out of the real user data directory.

## Build

PyInstaller cannot cross-compile, so run this on each target OS:

```powershell
uv run --group build python build.py
```

The build also verifies a real subtitle-to-subtitle sync through the packaged
worker, including the macOS `.app` executable. It needs no ffmpeg or network.
The onedir bundle lands in `dist/SubSyncr`. The executable wears three hats and
dispatches on its first argument: `--sync-worker` for the child process, a
subcommand for the CLI, and anything else for the GUI. ffmpeg is deliberately not
bundled. On macOS the same folder is also wrapped as `dist/SubSyncr.app` and
ad-hoc signed by PyInstaller. Signing or signature verification failures stop the
build. Release CI also verifies the extracted ZIP's signature and runs its worker.

The window and executable icons are rendered from the mark embedded in
`qt/icons.py`, so nothing has to be found on disk at runtime. After changing the
artwork, regenerate the `.ico` and `.icns` PyInstaller needs at build time with
`uv run python packaging/make_icon.py`.

## Where things are stored

`subsyncr doctor` prints the exact paths. Config is JSON, the queue and history
are SQLite, and cached speech arrays and downloaded engine binaries live under
the platform cache directory. The log file sits in the platform log directory
(on Windows, `%LOCALAPPDATA%\SubSyncr\SubSyncr\Logs\subsyncr.log`) and
rotates at midnight. Set `SUBSYNCR_DATA_DIR` to move all of it.

## Release and licensing

See [the changelog](CHANGELOG.md), [third-party notices](THIRD_PARTY.md), and
[the release checklist](docs/RELEASING.md). Release downloads include a SHA-256
manifest. The build collects dependency versions and license files into the bundle.

On Linux/macOS, creating a new output or backup safely requires a filesystem with
hard-link support; unsupported filesystems fail without replacing existing files.
Use a local data folder for the application queue and settings.
