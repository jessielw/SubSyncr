# Changelog

## [0.1.2] - 2026-11-20

### Fixed

- The window no longer stops responding while a batch runs. Each progress tick
  made the queue table re-measure its auto-sized columns across up to 1000 rows,
  about 140 ms per tick on a long queue. The table now measures only the rows on
  screen, so a tick costs about 5 ms.
- Worker events (progress, log lines, job updates) now reach the window in
  batches, at most about ten times a second, not one repaint and status recount
  per event. A busy batch can no longer pile up a backlog the window can't get
  through. The log pane adds each batch in one go, and if a tool floods it, only
  the newest lines are kept (the log file still has every line).
- A finishing group no longer resets the queue table, so the selection and
  scroll position stay where they were.
- Cancel running no longer freezes the window. Stopping a worker can take up
  to 7 seconds (grace period, then terminate, then kill), and this ran on the
  GUI thread, one worker after another. Workers now stop in parallel in the
  background. Quitting still waits for them, with the window hidden, up to a
  single 10-second limit.
- Starting a group no longer holds up the window while it checks for clashing
  output paths. That check resolved and stat'ed every path of every other
  queued job while holding the lock the window reads the queue through. It now
  runs outside the lock and reads each path from disk once.
- Saving the queue to its database no longer holds that lock either, so the
  window doesn't wait on SQLite while workers claim or finish groups.
- The right-click Sync against menu no longer runs ffprobe on the GUI thread
  (once per selected video, slow on a network share). It uses tracks it has
  already read; for the rest it shows "Reading tracks..." and fills itself in
  once a background read finishes.
- Match folders scans the folders and matches the files on a background thread,
  so a large or networked folder no longer freezes the dialog.

## [0.1.1] - 2026-11-12

### Changed

- Skip Signs & Songs and forced-titled PGS tracks when choosing the reference
  automatically, even when the release leaves them unflagged or makes them default.
- Settle each job's reference track (audio, PGS, or extracted text) when it is
  queued rather than mid-run, so the speech cache is keyed by the track actually
  used. A retry no longer reuses timings cached from the wrong track, and arrays
  built from an extracted subtitle are no longer mixed up with audio ones.
- Show each job's reference track in a new Track column, and change it from the
  queue's right-click menu (Sync against: Automatic, Audio, or any track in the
  video). Picking a track for a failed job queues it again.
- Open file and folder dialogs in the folder last dropped on the window or browsed
  to, remembered across restarts. A field that already names a path still wins.
- Name the track a failed ffsubsync alignment used (e.g. "against PGS · eng (s:0)"),
  and point the Result tooltip at Sync against so another track is one click away.
- A video whose tracks cannot be read when it is queued (ffprobe missing, a network
  share still waking up) is no longer settled as Audio for good: the track is picked
  when the job runs, and Retry reads the video again.
- Sync against works across several videos: it offers each track language every
  selected video has in full (e.g. "PGS · eng") and gives each episode its own
  matching stream, so a whole season can be switched in one click.

## [0.1.0] - 2026-10-01

First public release of SubSyncr.

- Review folder matches by episode identifier, normalized name, or fuzzy similarity;
  queue multiple languages and forced/SDH variants against one reference.
- Synchronize with ffsubsync, alass, lapse, or autosubsync, with an optional fallback
  chain, embedded subtitle references, PGS timings, and reusable speech analysis.
- Run parallel jobs, cancel groups, retry failures, recover pending work after a
  restart, and search completed-job history.
- Write through temporary files; preserve existing output on failed or low-quality
  syncs; require a usable backup when backup-before-overwrite is enabled. Reject
  conflicting queue destinations and retain language tags in video-based names.
- Follow the system's light/dark appearance, copy support diagnostics, and access
  help and issue reporting from the Help menu.

Portable builds target Windows x64, Linux x64, and macOS arm64/x64. Audio references
require an external ffmpeg/ffprobe installation. macOS builds are ad-hoc signed,
without Developer ID signing or notarization; Windows builds are unsigned. The Windows
CLI requires a source installation; the packaged Windows executable is a GUI app.
