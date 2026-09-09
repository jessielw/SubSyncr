"""Turn reviewed match proposals into queueable jobs."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from pathlib import Path

from subsyncr.core.models import (
    MatchResult,
    SyncJob,
    SyncOptions,
    resolve_output,
)


def group_key_for(video: Path) -> str:
    """Stable id shared by every subtitle synced against one video.

    Jobs sharing a key can share a serialized speech array, so only the first
    of them pays to extract audio.
    """
    try:
        identity = str(video.resolve())
    except OSError:
        identity = str(video)
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:16]


def build_job(
    video: Path,
    subtitle: Path,
    options: SyncOptions,
    *,
    output_suffix: str = ".synced",
    language: str | None = None,
    group_key: str | None = None,
) -> SyncJob:
    return SyncJob(
        reference_path=video,
        subtitle_path=subtitle,
        output_path=resolve_output(video, subtitle, options.files, output_suffix),
        options=options.copy(),
        language=language,
        group_key=group_key if group_key is not None else group_key_for(video),
    )


def jobs_from_matches(
    matches: Iterable[MatchResult],
    options: SyncOptions,
    *,
    output_suffix: str = ".synced",
    min_confidence: float = 0.0,
) -> list[SyncJob]:
    """Build one job per matched subtitle, skipping unmatched proposals."""
    jobs: list[SyncJob] = []
    keys: dict[Path, str] = {}
    for match in matches:
        if match.subtitle is None or match.confidence < min_confidence:
            continue
        key = keys.setdefault(match.video, group_key_for(match.video))
        jobs.append(
            build_job(
                match.video,
                match.subtitle,
                options,
                output_suffix=output_suffix,
                language=match.language,
                group_key=key,
            )
        )
    return jobs
