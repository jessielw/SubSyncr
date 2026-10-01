# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys
import sysconfig

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent

# The engine registry is importable on its own - it deliberately does not import
# the implementations - so the spec can ask it which modules to bundle rather
# than keep a second copy of the list that silently rots.
sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root / "packaging"))
from release_support import collect_notices, windows_version  # noqa: E402
from subsyncr import __version__  # noqa: E402
from subsyncr.core.engines import all_specs  # noqa: E402

# Regenerated from the embedded mark by packaging/make_icon.py. macOS wants the
# .icns for the .app; everywhere else takes the .ico.
icon = root / "packaging" / "icons" / (
    "subsyncr.icns" if sys.platform == "darwin" else "subsyncr.ico"
)

ffsubsync_datas, ffsubsync_binaries, ffsubsync_hiddenimports = collect_all("ffsubsync")
auto_datas, auto_binaries, auto_hiddenimports = collect_all("autosubsync")

datas = [*ffsubsync_datas, *auto_datas]

# autosubsync installs its classifier at the root of site-packages instead of
# inside the package, so collect_all never sees it. It goes to the root of the
# bundle, which is where engines.autosubsync.model_path() looks in a frozen
# build (sys._MEIPASS, and equally the parent of the collected package).
model = Path(sysconfig.get_path("purelib")) / "trained-model.bin"
if not model.is_file():
    raise SystemExit(f"autosubsync's trained model is missing: {model}")
datas.append((str(model), "."))

hiddenimports = [
    *ffsubsync_hiddenimports,
    *auto_hiddenimports,
    # These three are imported lazily inside subsyncr.__main__ so that the
    # sync worker never pays to load Qt. Name them explicitly rather than rely
    # on PyInstaller following imports that sit inside a function body.
    "subsyncr.core.worker",
    "subsyncr.cli.main",
    "subsyncr.qt.app",
    # The worker resolves an engine by the dotted path recorded in its registry
    # entry, so no import statement anywhere names these for PyInstaller to
    # follow. Without them a packaged build reports every engine as unavailable.
    *(engine.module for engine in all_specs()),
]

a = Analysis(
    [str(root / "src" / "subsyncr" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[*ffsubsync_binaries, *auto_binaries],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    # ffsubsync only needs torch for the silero and fused VAD backends, which
    # we do not expose. Bundling it would add hundreds of megabytes. sklearn is
    # only reachable from autosubsync's training path, never from syncing.
    excludes=["torch", "tkinter", "sklearn", "scikit-learn"],
    noarchive=False,
    optimize=0,
)
# Inventory exactly the distributions Analysis selected, including lazy engine
# imports. Metadata supplies versions for Help > Copy diagnostics when frozen.
a.datas += collect_notices(root, [entry[0] for entry in a.pure])
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SubSyncr",
    icon=str(icon),
    version=str(windows_version(root, __version__)) if sys.platform == "win32" else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # PyInstaller automatically ad-hoc signs on macOS.
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SubSyncr",
)

# A bare onedir folder is not something Finder will launch as an app, so on
# macOS the same folder is also wrapped as SubSyncr.app. The worker re-exec
# still works from inside it: sys.executable is Contents/MacOS/SubSyncr. The
# bundle is ad-hoc signed, without Developer ID signing or notarization.
# Gatekeeper still requires the user to allow it; the README says how.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SubSyncr.app",
        icon=str(icon),
        bundle_identifier="io.github.jessielw.subsyncr",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "SubSyncr",
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            # Follow the system's light/dark setting rather than forcing light.
            "NSRequiresAquaSystemAppearance": False,
        },
    )
