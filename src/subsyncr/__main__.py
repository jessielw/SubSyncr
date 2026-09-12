"""Process entry point.

One executable wears three hats, and the order below matters. The sync-worker
branch must be taken before anything imports PySide6, so that the packaged
application can re-exec itself as a lightweight child process without paying
for - or requiring - a GUI toolkit.
"""

from __future__ import annotations

import multiprocessing
import sys

WORKER_FLAG = "--sync-worker"
CLI_FLAG = "--cli"


def main(argv: list[str] | None = None) -> int:
    # Must be the first thing that happens in a frozen process.
    multiprocessing.freeze_support()
    # Before any hat is chosen, so no library this process ends up importing
    # can flash a console window at the user.
    from subsyncr.core.spawn import ConsoleGuard

    ConsoleGuard.install()

    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments and arguments[0] == WORKER_FLAG:
        from subsyncr.core.worker import main as worker_main

        return worker_main(arguments[1:])

    if arguments and arguments[0] == CLI_FLAG:
        from subsyncr.cli.main import main as cli_main

        return cli_main(arguments[1:])

    # A bare subcommand also means CLI, so `subsyncr match ...` works.
    from subsyncr.cli.main import COMMANDS

    if arguments and arguments[0] in COMMANDS:
        from subsyncr.cli.main import main as cli_main

        return cli_main(arguments)

    from subsyncr.qt.app import run_gui

    return run_gui(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
