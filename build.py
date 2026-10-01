from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    build_env = os.environ.copy()
    if sys.platform == "darwin":
        # PyInstaller normally only warns if signing the outer .app fails.
        # Refuse to ship that bundle, and verify its nested signatures too.
        build_env["PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR"] = "1"
        build_env["PYINSTALLER_VERIFY_BUNDLE_SIGNATURE"] = "1"
    subprocess.run(
        ["uv", "sync", "--frozen", "--group", "build"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "--group",
            "build",
            "pyinstaller",
            "--clean",
            "--noconfirm",
            str(root / "packaging" / "subsyncr.spec"),
        ],
        cwd=root,
        env=build_env,
        check=True,
    )
    output = root / "dist" / "SubSyncr"
    if not output.exists():
        raise FileNotFoundError(f"PyInstaller output was not created: {output}")
    executable = output / ("SubSyncr.exe" if sys.platform == "win32" else "SubSyncr")
    if sys.platform == "darwin":
        output = root / "dist" / "SubSyncr.app"
        executable = output / "Contents" / "MacOS" / "SubSyncr"
    subprocess.run(
        [sys.executable, str(root / "packaging" / "smoke_test.py"), str(executable)],
        cwd=root,
        check=True,
    )
    print(f"Build and frozen sync verified: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
