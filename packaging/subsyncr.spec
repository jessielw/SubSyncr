# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
ffsubsync_datas, ffsubsync_binaries, ffsubsync_hiddenimports = collect_all("ffsubsync")

hiddenimports = [
    *ffsubsync_hiddenimports,
    # These three are imported lazily inside subsyncr.__main__ so that the
    # sync worker never pays to load Qt. Name them explicitly rather than rely
    # on PyInstaller following imports that sit inside a function body.
    "subsyncr.core.worker",
    "subsyncr.cli.main",
    "subsyncr.qt.app",
]

a = Analysis(
    [str(root / "src" / "subsyncr" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=ffsubsync_binaries,
    datas=ffsubsync_datas,
    hiddenimports=hiddenimports,
    hookspath=[str(root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    # ffsubsync only needs torch for the silero and fused VAD backends, which
    # we do not expose. Bundling it would add hundreds of megabytes.
    excludes=["torch", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SubSyncr",
    # Regenerated from the embedded mark by packaging/make_icon.py.
    icon=str(root / "packaging" / "icons" / "subsyncr.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SubSyncr",
)
