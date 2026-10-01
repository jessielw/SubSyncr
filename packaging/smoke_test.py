"""Exercise the shipped executable using only the build host's Python stdlib.

The control pipe stays open until the worker finishes: EOF means cancellation.
This also works with the windowed Windows executable, whose ordinary CLI has
no terminal streams. No ffmpeg, network, or user data directory is needed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    return (
        f"{milliseconds // 3600000:02}:{milliseconds // 60000 % 60:02}:"
        f"{milliseconds // 1000 % 60:02},{milliseconds % 1000:03}"
    )


def subtitles(path: Path, shift: float) -> None:
    position = 4.0
    blocks = []
    for index, step in enumerate((3.1, 5.7, 2.3, 8.2, 4.4, 6.9, 3.5) * 6, 1):
        blocks.append(
            f"{index}\n{timestamp(position + shift)} --> "
            f"{timestamp(position + shift + 1.4)}\nExample {index}\n"
        )
        position += step
    path.write_text("\n".join(blocks), encoding="utf-8")


def smoke_test(executable: Path, timeout: float = 60) -> None:
    executable = executable.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="subsyncr-smoke-") as scratch:
        root = Path(scratch)
        reference = root / "reference.srt"
        subtitles(reference, 0)
        items = []
        for language in ("en", "es"):
            source = root / f"input.{language}.srt"
            subtitles(source, 6.5)
            items.append(
                {
                    "id": language,
                    "subtitle": str(source),
                    "output": str(root / f"output.{language}.srt"),
                }
            )
        spec = {
            "engine": "ffsubsync",
            "reference": str(reference),
            "items": items,
            "options": {
                "files": {"encoding": "utf-8", "output_encoding": "utf-8"},
                "ffsubsync": {"no_fix_framerate": True, "pgs_ref_stream": ""},
            },
        }
        env = dict(os.environ, SUBSYNCR_DATA_DIR=str(root / "data"))
        # Prove a subtitle reference works without discovering a host ffmpeg.
        env["PATH"] = str(root)
        with (root / "stderr.txt").open("w+", encoding="utf-8") as stderr:
            process = subprocess.Popen(
                [str(executable), "--sync-worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            events: queue.Queue[str | None] = queue.Queue()

            def read() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    events.put(line)
                events.put(None)

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(spec) + "\n")
                process.stdin.flush()
                deadline = time.monotonic() + timeout
                results = {}
                while len(results) < len(items):
                    line = events.get(timeout=max(0.01, deadline - time.monotonic()))
                    if line is None:
                        break
                    event = json.loads(line)
                    if event.get("event") == "result":
                        results[event["job_id"]] = event
                if len(results) != len(items):
                    raise RuntimeError(f"Worker did not report every result: {results}")
                for item in items:
                    result = results[item["id"]]
                    if result.get("status") != "succeeded":
                        raise RuntimeError(f"Frozen sync failed: {result}")
                    offset = result.get("offset_seconds")
                    if offset is None or abs(float(offset) + 6.5) > 0.25:
                        raise RuntimeError(f"Wrong alignment offset: {result}")
                    output = Path(item["output"]).read_text(encoding="utf-8")
                    if "00:00:04," not in output or "Example 42" not in output:
                        raise RuntimeError(
                            "Output cues are missing or incorrectly shifted"
                        )
                process.stdin.close()
                if process.wait(timeout=10) != 0:
                    raise RuntimeError("Frozen worker returned an error exit code")
            except Exception:
                process.kill()
                process.wait(timeout=10)
                stderr.seek(0)
                print(stderr.read())
                raise
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                reader.join(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
    print(f"Frozen sync passed: {executable.name} (2 subtitles, -6.5s, no ffmpeg)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    smoke_test(parser.parse_args().executable)
