"""The autosubsync engine.

The odd one out: it aligns with a trained classifier over audio features rather
than by detecting speech boundaries, so it can find a shift where voice-activity
methods struggle. In exchange it only reads SubRip, and it cannot use a subtitle
as a reference at all - it needs the audio.

Two things about driving it are worth knowing.

**It has a real API.** ``synchronize(..., return_parameters=True)`` hands back
``(success, quality, skew, shift)``, which is everything the queue wants to
record. AutoSubSync drives it by rewriting ``sys.argv`` and running the module
through ``runpy`` with the file descriptors redirected; none of that is needed.

**Its packaged model cannot be found by default.** Left to itself the library
resolves the model through ``pkg_resources``, which is not importable on the
Python versions this project supports - so ``model_file`` is always passed
explicitly, resolved by ``model_path`` below.

It also shells out to a literally-named ``ffmpeg``, with no way to point it
elsewhere, so a configured ffmpeg folder is put on PATH for the call.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import sysconfig
from typing import Any

from ffsubsyncui.core.engines.base import EngineId
from ffsubsyncui.core.engines.runner import Outcome, run_each
from ffsubsyncui.core.worker import CancelToken, EventChannel

_MODEL = "trained-model.bin"


def model_path() -> Path | None:
    """Locate the classifier autosubsync ships beside its package.

    The library installs it at the root of site-packages, one level above the
    package itself, which is what its own ``resource_filename(__name__, '..')``
    lookup was reaching for before ``pkg_resources`` went away.
    """
    candidates: list[Path] = []
    try:
        import autosubsync

        package = Path(autosubsync.__file__).resolve().parent
        candidates.extend((package.parent / _MODEL, package / _MODEL))
    except (ImportError, TypeError, ValueError):
        pass
    # A frozen build unpacks data files beside the executable's bundle.
    if bundle := getattr(sys, "_MEIPASS", ""):
        candidates.append(Path(str(bundle)) / _MODEL)
    with_purelib = sysconfig.get_path("purelib")
    if with_purelib:
        candidates.append(Path(with_purelib) / _MODEL)
    return next((path for path in candidates if path.is_file()), None)


def _path_with_ffmpeg(ffmpeg_path: str) -> str | None:
    """PATH with the configured ffmpeg folder in front, or None to leave it be.

    autosubsync runs ``ffmpeg`` by bare name, so this is the only way to make
    it use anything other than whatever PATH already resolves to.
    """
    configured = ffmpeg_path.strip()
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    directory = candidate.parent if candidate.is_file() else candidate
    if not directory.is_dir():
        return None
    return os.pathsep.join((str(directory), os.environ.get("PATH", "")))


def align(
    reference: str,
    subtitle: Path,
    output: Path,
    options: dict[str, Any],
    quality_threshold_reason: str = "quality of fit below autosubsync's threshold",
) -> Outcome:
    """Align one subtitle and report what came back."""
    from autosubsync.main import synchronize

    tuning: dict[str, Any] = options.get("autosubsync") or {}
    environment: dict[str, Any] = options.get("environment") or {}

    model = model_path()
    if model is None:
        return Outcome(
            status="failed",
            error=(
                "autosubsync's trained model could not be found; "
                "reinstall the autosubsync package"
            ),
        )

    original_path = os.environ.get("PATH", "")
    patched = _path_with_ffmpeg(str(environment.get("ffmpeg_path", "")))
    if patched is not None:
        os.environ["PATH"] = patched
    try:
        success, quality, skew, shift = synchronize(
            reference,
            str(subtitle),
            str(output),
            verbose=False,
            model_file=str(model),
            max_shift_secs=float(tuning.get("max_shift_secs", 20.0)),
            parallelism=max(1, int(tuning.get("parallelism", 3))),
            fixed_skew=tuning.get("fixed_skew"),
            return_parameters=True,
        )
    finally:
        os.environ["PATH"] = original_path

    outcome = Outcome(
        status="succeeded",
        offset_seconds=float(shift) if shift is not None else None,
        # autosubsync calls it skew; it is the same playback-rate ratio the
        # other engines report as a framerate scale factor.
        framerate_scale_factor=(
            float(skew) if skew is not None and float(skew) != 1.0 else None
        ),
        score=float(quality) if quality is not None else None,
    )
    if not success:
        # autosubsync writes the shifted file regardless of how well it fit, so
        # a poor fit would otherwise silently replace a good subtitle with a
        # wrong one. Reporting it here leaves the staged file unpromoted.
        outcome.status = "skipped_low_quality"
        outcome.quality_reasons = [quality_threshold_reason]
    return outcome


def run_group(spec: dict[str, Any], channel: EventChannel, cancel: CancelToken) -> int:
    def run_one(
        reference: str,
        subtitle: Path,
        staging: Path,
        options: dict[str, Any],
        job_id: str,
    ) -> Outcome:
        del job_id
        return align(reference, subtitle, staging, options)

    return run_each(spec, channel, cancel, engine=EngineId.AUTOSUBSYNC, run_one=run_one)
