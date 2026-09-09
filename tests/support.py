"""Helpers shared by tests, importable (unlike fixtures) at module scope."""

from __future__ import annotations

from ffsubsyncui.core.models import FFSubSyncOptions, FileOptions, SyncOptions


def sync_options(**overrides: object) -> SyncOptions:
    """Build ``SyncOptions`` from flat keyword arguments.

    Options are nested per engine, which is right for the app and tedious in a
    test that only wants to say "vad is auditok". Each name is routed to the
    block that owns it, and an unknown name is a mistake rather than a silent
    no-op.
    """
    # Properties count: overwrite_input is a writable view of output_policy.
    file_fields = set(FileOptions.__dataclass_fields__) | {
        name
        for name, member in vars(FileOptions).items()
        if isinstance(member, property) and member.fset is not None
    }
    tuning_fields = set(FFSubSyncOptions.__dataclass_fields__)
    options = SyncOptions()
    for name, value in overrides.items():
        if name in file_fields:
            setattr(options.files, name, value)
        elif name in tuning_fields:
            setattr(options.ffsubsync, name, value)
        elif name == "ffmpeg_path":
            options.environment.ffmpeg_path = str(value)
        else:
            raise AssertionError(f"unknown option {name!r}")
    return options
