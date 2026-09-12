"""Keeping console windows from flashing over the GUI.

On Windows every console program - ffmpeg, ffprobe, alass, lapse - is handed a
console window when it starts, and a windowed process has none to lend it, so
each one opens its own. ``CREATE_NO_WINDOW`` suppresses that, and everything
this project spawns itself passes it, via ``creation_flags`` below.

That is only half the problem. The ffsubsync and autosubsync engines are
libraries we drive in-process, and they shell out to ffmpeg with their own
hardcoded ``subprocess`` calls that take no flags from us. In development they
are covered by accident: the worker is ``python.exe``, a console program started
with ``CREATE_NO_WINDOW``, and the hidden console it is given is inherited by
everything it spawns. In a frozen build it is not. There the worker is the
windowed executable re-exec'ing itself, ``CREATE_NO_WINDOW`` means nothing to a
GUI-subsystem process, and so there is no console to inherit - every ffmpeg the
engines start pops a window in the user's face.

``ConsoleGuard`` closes that gap by making "no console window" the default for
the whole process: any ``Popen`` that has not already said something about
windows gets ``CREATE_NO_WINDOW`` added. It is deliberately conservative - a
caller that passed ``startupinfo``, or asked for a new or detached console, is
left exactly as it is - and it does nothing at all off Windows, where none of
this applies.
"""

from __future__ import annotations

from collections.abc import Callable
import inspect
import os
import subprocess
from typing import Any


def creation_flags() -> int:
    """``CreateProcess`` flags for a tool that should stay out of sight."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _decided_flags() -> int:
    """The bits that mean the caller has already chosen about consoles.

    ``CREATE_NEW_CONSOLE`` and ``DETACHED_PROCESS`` are both incompatible with
    ``CREATE_NO_WINDOW``, so a caller asking for either is left alone rather
    than overruled into an error.
    """
    return (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )


def hide_window(creationflags: int = 0, startupinfo: Any = None) -> int:
    """The flags a spawn should end up with, given what it asked for.

    ``startupinfo`` carries its own window handling through
    ``STARTF_USESHOWWINDOW``; ffsubsync's ffmpeg calls already arrive that way,
    so they are passed through untouched.
    """
    if startupinfo is not None or creationflags & _decided_flags():
        return creationflags
    return creationflags | creation_flags()


class ConsoleGuard:
    """The process-wide "no console window" default for spawning.

    There is one ``subprocess`` module to patch per process, so the guard is
    the class itself rather than something anyone instantiates. Keeping the
    state here is also what makes it removable: the saved function is the only
    record of what the interpreter looked like before we touched it.
    """

    # The unpatched Popen.__init__, held while the guard is installed. Its
    # presence is what "installed" means, so the two cannot drift apart.
    _original: Callable[..., None] | None = None

    @classmethod
    def installed(cls) -> bool:
        return cls._original is not None

    @classmethod
    def install(cls) -> None:
        """Default every subprocess in this process to no console window.

        A no-op off Windows, and idempotent.
        """
        if cls.installed() or os.name != "nt":
            return
        original = subprocess.Popen.__init__
        # Popen takes startupinfo and creationflags positionally as well as by
        # keyword, and third-party code is under no obligation to be tidy about
        # it. Binding against the real signature is the only way to read what a
        # caller passed without caring how they passed it.
        signature = inspect.signature(original)

        def patched(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
            bound = signature.bind(self, *args, **kwargs)
            bound.arguments["creationflags"] = hide_window(
                int(bound.arguments.get("creationflags") or 0),
                bound.arguments.get("startupinfo"),
            )
            original(*bound.args, **bound.kwargs)

        cls._original = original
        subprocess.Popen.__init__ = patched  # type: ignore[method-assign]

    @classmethod
    def uninstall(cls) -> None:
        """Put the untouched ``Popen`` back. Mainly so tests leave no trace."""
        if cls._original is None:
            return
        subprocess.Popen.__init__ = cls._original  # type: ignore[method-assign]
        cls._original = None
