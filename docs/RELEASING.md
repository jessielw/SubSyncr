# Releasing SubSyncr

## Prepare the candidate

1. Keep package/runtime versions equal, update `CHANGELOG.md`, and run
   `uv sync --frozen --group dev --group build`.
2. Run `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`,
   `uv run --no-sync basedpyright`, and `uv run --no-sync pytest`.
3. Build with `uv run --group build python build.py`. This runs an actual frozen
   two-subtitle alignment without ffmpeg. On macOS it tests the executable inside
   the shipped `.app` and requires valid ad-hoc signatures. Signing, signature
   verification, or smoke-test failures fail the build. macOS release CI extracts
   the final ZIP, verifies its nested signatures, and reruns the frozen sync.
4. Inspect the bundle's `THIRD_PARTY_INVENTORY.json`, package license files, Qt
   library attribution, and font notices. Refresh `packaging/licenses` and its
   provenance hashes if dependencies change. Verify exact source availability
   and redistribution requirements for the libraries in each platform archive.
5. Run the Release workflow manually to generate candidate artifacts for all four
   targets. Check every job and test the downloaded archives before tagging.

## Clean-machine checks (each supported platform)

Record OS version, architecture, archive SHA-256, and results in the draft release.
Allow 30-60 minutes per platform. These checks require actual target machines or VMs;
local source tests and a successful build do not substitute for them.

- [ ] Extract the archive; launch without Python or ffmpeg installed. On macOS,
      verify documented Gatekeeper steps and both Intel/Apple Silicon artifacts.
- [ ] Sync two subtitle files with a known offset. Verify output timing, Unicode
      text, and logs. Open a second GUI and confirm it refuses the same data folder.
- [ ] Try an audio reference without ffmpeg; configure its folder and rerun. Check
      alass/lapse first-use downloads and a network failure with a useful error.
- [ ] Cancel a running group, retry, quit, reopen, and check pending work/history.
      Confirm low-quality/failed jobs preserve existing outputs and originals.
- [ ] Review matching and settings at 100% and 150% scaling in light/dark themes;
      test drag/drop, keyboard access, Help links, and Copy diagnostics.

## Publish deliberately

1. Review `git status` and the intended public history. Local handoff files,
   assistant state, environments, credentials, and media files must stay excluded.
   Inspect the public author/contact metadata. Never push local `backup-*` tags
   as part of the release; push only the specific `v<version>` tag.
2. After checks pass and publication is authorized, create and push that tag.
   The workflow assembles a **draft** release with the platform archives and a
   `SHA256SUMS.txt` manifest. Check the version displayed by each app.
3. Copy the relevant changelog entry into the draft; record clean-machine results
   and known limitations. Check the SHA-256 manifest against downloaded artifacts.
4. Publish the release and change repository visibility only after reviewing the
   final artifacts. Apple Developer ID signing/notarization remains a separate
   release decision; 0.1.0 uses PyInstaller's ad-hoc signing on macOS. Ad-hoc signing
   does not establish developer identity or remove Gatekeeper's first-launch check.

## Known deployment limits

On Windows, `SubSyncr.exe` is windowed; use a source install for terminal CLI output.
On Linux/macOS, safe creation of a new output/backup uses hard links on the target
filesystem. If that filesystem does not support them, the operation fails safely;
choose a local filesystem that supports hard links. Explicit replacement uses an
atomic rename. Keep the app data directory on a local disk for GUI instance locking.
