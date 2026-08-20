# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
ffsubsync_datas, ffsubsync_binaries, ffsubsync_hiddenimports = collect_all("ffsubsync")

a = Analysis(
    [str(root / "src" / "ffsubsyncui" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=ffsubsync_binaries,
    datas=ffsubsync_datas,
    hiddenimports=ffsubsync_hiddenimports,
    hookspath=[str(root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FFSubSyncUI",
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
    name="FFSubSyncUI",
)
