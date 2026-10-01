# Changelog

## 0.1.0

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
