from __future__ import annotations

from pathlib import Path
import subprocess


def main() -> int:
    root = Path(__file__).resolve().parent
    subprocess.run(
        ["uv", "sync", "--frozen", "--group", "build"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--group",
            "build",
            "pyinstaller",
            "--clean",
            "--noconfirm",
            str(root / "packaging" / "ffsubsyncui.spec"),
        ],
        cwd=root,
        check=True,
    )
    output = root / "dist" / "FFSubSyncUI"
    if not output.exists():
        raise FileNotFoundError(f"PyInstaller output was not created: {output}")
    print(f"Build complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
