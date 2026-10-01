"""Publish complete subtitles without sacrificing an existing file on failure.

Every engine writes into its own temporary directory on the destination's
filesystem. Renaming is atomic; creating a hard link provides the corresponding
atomic *no replacement* operation, including when another process writes after
validation. Filesystems without hard-link support fail safely in that mode.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from subsyncr.core.models import FileOptions


def publish_new(staged: Path, target: Path) -> None:
    """Atomically create a destination, refusing an existing name."""
    if os.name == "nt":
        # Windows rename refuses replacement, including on FAT/exFAT where
        # hard links are unavailable. POSIX rename would replace the target.
        os.rename(staged, target)
    else:
        os.link(staged, target)


def same_path(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def backup_original(subtitle: Path) -> Path:
    """Publish a complete backup exclusively; never replace an earlier backup."""
    target = subtitle.with_suffix(subtitle.suffix + ".bak")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.stat().st_size == 0:
            raise OSError(f"Backup is not a usable file: {target}")
        return target
    with tempfile.TemporaryDirectory(
        prefix=".subsyncr-backup-", dir=subtitle.parent
    ) as d:
        staged = Path(d) / subtitle.name
        shutil.copy2(subtitle, staged)
        try:
            publish_new(staged, target)
        except FileExistsError:
            # Another process may have published first. Validate its completed
            # backup by the same rules, without ever replacing it.
            return backup_original(subtitle)
    return target


class OutputTransaction:
    """Own the staging file and promote it only after a successful alignment."""

    def __init__(self, subtitle: Path, output: Path, files: FileOptions) -> None:
        self.subtitle = subtitle
        self.output = subtitle if files.overwrite_input else output
        self.files = files
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.staging = self.output

    def __enter__(self) -> OutputTransaction:
        if not self.files.overwrite_input and same_path(self.subtitle, self.output):
            raise ValueError(
                "Output is the input subtitle; choose Overwrite explicitly"
            )
        if self.files.overwrite_input and self.files.backup_before_overwrite:
            try:
                backup_original(self.subtitle)
            except OSError as exc:
                raise OSError(
                    f"Could not secure a backup; original left untouched: {exc}"
                ) from exc
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(
            prefix=".subsyncr-output-", dir=self.output.parent
        )
        self.staging = Path(self._temporary.name) / self.output.name
        return self

    def commit(self) -> None:
        if not self.staging.is_file() or self.staging.stat().st_size == 0:
            raise OSError("The engine did not produce a nonempty subtitle")
        if self.files.overwrite_input or self.files.replace_output:
            os.replace(self.staging, self.output)
        else:
            # Unlike an exists()/replace() pair, this cannot overwrite a file
            # another job or application created while alignment was running.
            publish_new(self.staging, self.output)

    def __exit__(self, *exc: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
