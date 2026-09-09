"""Finding, and if necessary fetching, the native tools some engines are.

alass and lapse are compiled programs rather than Python packages, so they
cannot ride along as dependencies. They are resolved in this order:

1. **PATH** - an alass the user installed themselves wins, because they chose
   it. On macOS this is the only route for alass, which publishes no build for
   it (``brew install alass``).
2. **Our cache** - a copy fetched by an earlier run, pinned to a known version.
3. **Download** - fetched from the project's own GitHub release, verified
   against a checksum recorded here, and unpacked atomically.

Downloading, never bundling, is a deliberate choice: alass is GPL-3.0 and
shipping its binary inside an MIT application would drag redistribution
obligations along with it. Fetched at the user's request from upstream, it stays
a separate program we merely invoke.

Nothing here runs automatically. ``status`` answers "is this usable?" without
touching the network; only ``ensure`` downloads, and only when an engine is
actually asked to run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

from subsyncr.core.engines import EngineId

# Bytes per read while downloading. Large enough to be efficient, small enough
# that progress moves and a cancel is noticed promptly.
_CHUNK = 1 << 16

_TIMEOUT = 60.0

ProgressCallback = Callable[[int, int | None], None]


class BinaryError(RuntimeError):
    """A required native tool is missing and could not be obtained."""


@dataclass(frozen=True, slots=True)
class Asset:
    """One downloadable file, and how to get an executable out of it."""

    url: str
    # SHA-256 of the downloaded file. Recorded when the project publishes one
    # or verified by hand at the pinned version; a mismatch is always fatal.
    sha256: str
    # Path of the executable inside the archive. Empty means the download *is*
    # the executable.
    member: str = ""

    @property
    def archived(self) -> bool:
        return bool(self.member)


@dataclass(frozen=True, slots=True)
class BinarySpec:
    """Everything needed to find or fetch one engine's native tool."""

    engine: EngineId
    version: str
    # What the executable is called on PATH.
    command: str
    homepage: str
    # Keyed by (system, machine) as normalised by ``platform_key``.
    assets: Mapping[tuple[str, str], Asset] = field(default_factory=dict)
    # Shown when there is no asset for this platform and PATH came up empty.
    install_hint: str = ""


@dataclass(frozen=True, slots=True)
class BinaryStatus:
    """Whether a tool is usable right now, and where it came from."""

    engine: EngineId
    path: Path | None
    source: str  # "path" | "cache" | "downloadable" | "unavailable"
    message: str

    @property
    def ready(self) -> bool:
        return self.path is not None

    @property
    def can_download(self) -> bool:
        return self.source == "downloadable"


def platform_key() -> tuple[str, str]:
    """This machine, in the vocabulary the asset tables use."""
    machine = platform.machine().lower()
    if machine in {"x86_64", "x64", "amd64"}:
        machine = "amd64"
    elif machine in {"aarch64", "arm64", "armv8l", "armv8b"}:
        machine = "arm64"
    return platform.system(), machine


_ALASS_RELEASE = "https://github.com/kaegi/alass/releases/download/v2.0.0"
_LAPSE_RELEASE = "https://github.com/Schwponaco-org/lapse/releases/download/v2.0.4"

ALASS = BinarySpec(
    engine=EngineId.ALASS,
    version="2.0.0",
    command="alass-cli",
    homepage="https://github.com/kaegi/alass",
    assets={
        # The Windows archive carries its own ffmpeg beside the binary; we
        # unpack the lot and point alass at it when the user has none.
        ("Windows", "amd64"): Asset(
            url=f"{_ALASS_RELEASE}/alass-windows64.zip",
            sha256="e81a72f97f592910e909a2352d6b8c0de0801c51ac1383bad4ebf3f2ecdd2fd8",
            member="alass-windows64/bin/alass-cli.exe",
        ),
        ("Linux", "amd64"): Asset(
            url=f"{_ALASS_RELEASE}/alass-linux64",
            sha256="7bd0b9ae7e035d3ba940eacffb21243614df36231d47f21f0b4ce42001ab7fcd",
        ),
    },
    # alass publishes no macOS build at all.
    install_hint="alass has no macOS build; install it with `brew install alass`.",
)

LAPSE = BinarySpec(
    engine=EngineId.LAPSE,
    version="2.0.4",
    command="lapse",
    homepage="https://github.com/Schwponaco-org/lapse",
    # Checksums are lapse's own published SHA256SUMS for this release.
    assets={
        ("Windows", "amd64"): Asset(
            url=f"{_LAPSE_RELEASE}/lapse-windows-x64.exe",
            sha256="5329a8f70d9089b474f3df9778834a3878a962670ab0230a082862dae6f2378e",
        ),
        # Windows on ARM runs the x64 build under emulation.
        ("Windows", "arm64"): Asset(
            url=f"{_LAPSE_RELEASE}/lapse-windows-x64.exe",
            sha256="5329a8f70d9089b474f3df9778834a3878a962670ab0230a082862dae6f2378e",
        ),
        ("Linux", "amd64"): Asset(
            url=f"{_LAPSE_RELEASE}/lapse-linux-amd64",
            sha256="d7d729aa56eebb3b9b046082dd3bb0e63601a8d2c19c11f9087329e2b0e873c6",
        ),
        ("Linux", "arm64"): Asset(
            url=f"{_LAPSE_RELEASE}/lapse-linux-arm64",
            sha256="c6f24305de27bf8a447270552bb59e6a0174ffa3fbf44db7b04e7b5951c74efc",
        ),
        ("Darwin", "arm64"): Asset(
            url=f"{_LAPSE_RELEASE}/lapse-macos-arm64",
            sha256="8047b58f4e36c605fb3ea248c6103d2a6ef2974b31f11bcae80a1014f20ef971",
        ),
        ("Darwin", "amd64"): Asset(
            url=f"{_LAPSE_RELEASE}/lapse-macos-x86_64",
            sha256="e5a005f6a4b611968f00e5246bf09cafda3fd65898b6fd0b4e438c92b5bfabb8",
        ),
    },
)

SPECS: Mapping[EngineId, BinarySpec] = {ALASS.engine: ALASS, LAPSE.engine: LAPSE}


def spec_for(engine: EngineId) -> BinarySpec | None:
    """The native-tool spec for an engine, or None if it needs no binary."""
    return SPECS.get(engine)


def install_root(cache_root: Path, spec: BinarySpec) -> Path:
    """Where this exact version is unpacked. Version in the path, so an
    upgrade lands beside the old copy rather than fighting a running process."""
    return cache_root / "tools" / spec.engine.value / spec.version


def _executable_name(spec: BinarySpec, asset: Asset | None) -> str:
    if asset is not None and asset.archived:
        return Path(asset.member).name
    suffix = ".exe" if platform.system() == "Windows" else ""
    return f"{spec.command}{suffix}"


def installed_path(cache_root: Path, spec: BinarySpec) -> Path | None:
    """Our own copy, if a previous run put one there."""
    asset = spec.assets.get(platform_key())
    candidate = install_root(cache_root, spec) / _executable_name(spec, asset)
    return candidate if candidate.is_file() else None


def status(spec: BinarySpec, cache_root: Path) -> BinaryStatus:
    """Where this tool would come from, without touching the network."""
    if found := shutil.which(spec.command):
        return BinaryStatus(spec.engine, Path(found), "path", f"found on PATH: {found}")
    if existing := installed_path(cache_root, spec):
        return BinaryStatus(
            spec.engine, existing, "cache", f"downloaded copy: {existing}"
        )
    if spec.assets.get(platform_key()) is not None:
        return BinaryStatus(
            spec.engine,
            None,
            "downloadable",
            f"not installed; will be downloaded from {spec.homepage} on first use",
        )
    system, machine = platform_key()
    hint = spec.install_hint or f"install {spec.command} and put it on PATH"
    return BinaryStatus(
        spec.engine,
        None,
        "unavailable",
        f"no {spec.command} build for {system}/{machine}. {hint}",
    )


def resolve(spec: BinarySpec, cache_root: Path) -> Path | None:
    """An existing executable, without downloading. None if there is none."""
    return status(spec, cache_root).path


def ensure(
    spec: BinarySpec,
    cache_root: Path,
    *,
    on_progress: ProgressCallback | None = None,
    allow_download: bool = True,
) -> Path:
    """The executable, downloading it if that is the only way to have one."""
    current = status(spec, cache_root)
    if current.path is not None:
        return current.path
    if not allow_download or not current.can_download:
        raise BinaryError(current.message)
    asset = spec.assets[platform_key()]
    target = install_root(cache_root, spec)
    _install(
        asset,
        target,
        _executable_name(spec, asset),
        on_progress=on_progress,
    )
    installed = installed_path(cache_root, spec)
    if installed is None:
        raise BinaryError(
            f"{spec.command} was downloaded but no executable appeared in {target}"
        )
    return installed


# -- download and unpack -------------------------------------------------


def _install(
    asset: Asset,
    target: Path,
    executable_name: str,
    *,
    on_progress: ProgressCallback | None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=str(target.parent), prefix=".download-"
    ) as scratch:
        workspace = Path(scratch)
        payload = workspace / "payload"
        _download(asset.url, payload, on_progress=on_progress)
        _verify(payload, asset)

        staged = workspace / "staged"
        staged.mkdir()
        if asset.archived:
            _unpack(payload, staged)
            _flatten(staged, asset)
        else:
            # The release asset is named for its platform (lapse-windows-x64.exe);
            # on disk it takes the plain command name the resolver looks for.
            payload.replace(staged / executable_name)

        for item in staged.rglob("*"):
            if item.is_file():
                _make_executable(item)
        _publish(staged, target)


def _download(
    url: str, destination: Path, *, on_progress: ProgressCallback | None
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "subsyncr"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.headers.get("Content-Length")
            total = int(raw) if raw and raw.isdigit() else None
            received = 0
            with destination.open("wb") as handle:
                while chunk := response.read(_CHUNK):
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
    except urllib.error.URLError as exc:
        raise BinaryError(f"could not download {url}: {exc.reason}") from exc
    except OSError as exc:
        raise BinaryError(f"could not download {url}: {exc}") from exc


def _verify(payload: Path, asset: Asset) -> None:
    """A checksum mismatch is always fatal - never fall back to running it."""
    digest = hashlib.sha256()
    with payload.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != asset.sha256:
        raise BinaryError(
            f"checksum mismatch for {asset.url}: expected {asset.sha256}, got {actual}"
        )


def _unpack(payload: Path, destination: Path) -> None:
    if zipfile.is_zipfile(payload):
        with zipfile.ZipFile(payload) as archive:
            _extract_safely(destination, archive.namelist(), archive.extractall)
        return
    try:
        with tarfile.open(payload) as archive:
            _extract_safely(
                destination,
                archive.getnames(),
                lambda path: archive.extractall(path, filter="data"),
            )
    except tarfile.TarError as exc:
        raise BinaryError(f"could not unpack {payload.name}: {exc}") from exc


def _extract_safely(
    destination: Path, names: list[str], extract: Callable[[str], None]
) -> None:
    """Refuse an archive whose members would escape the target directory."""
    root = destination.resolve()
    for name in names:
        resolved = (root / name).resolve()
        if resolved != root and root not in resolved.parents:
            raise BinaryError(f"archive member escapes the install directory: {name}")
    extract(str(destination))


def _flatten(staged: Path, asset: Asset) -> Path:
    """Lift the executable to the top of the install directory.

    Archives normally wrap everything in one folder named for the release. That
    wrapper is unwrapped and the executable moved to the top, so the path we
    record does not depend on the archive's internal layout. Everything shipped
    alongside it is kept: alass bundles an ffmpeg build that we hand back to it
    at run time when the user has none of their own.
    """
    relative = Path(asset.member)
    entries = list(staged.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        wrapper = entries[0]
        for child in list(wrapper.iterdir()):
            child.replace(staged / child.name)
        wrapper.rmdir()
        if len(relative.parts) > 1:
            relative = Path(*relative.parts[1:])

    executable = staged / relative
    if not executable.is_file():
        raise BinaryError(f"{asset.member} was not in the archive at {asset.url}")
    final = staged / executable.name
    if executable != final:
        executable.replace(final)
        # The directory it came from is usually now empty; tidy it away, but
        # never at the cost of anything else the archive placed there.
        with contextlib.suppress(OSError):
            executable.parent.rmdir()
    return final


def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return  # Windows decides by extension, not by mode bits.
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass  # A tool we cannot chmod will fail loudly when it is run.


def _publish(staged: Path, target: Path) -> None:
    """Move the finished install into place, losing a race gracefully."""
    try:
        staged.replace(target)
    except OSError:
        if target.is_dir():
            return  # Another worker installed it first; theirs is as good.
        raise


# -- what an engine needs at run time ------------------------------------


def alass_environment(executable: Path, ffmpeg_directory: str = "") -> dict[str, str]:
    """Environment for an alass run.

    alass shells out to ffmpeg and takes its location from two environment
    variables pointing at the *binaries*, not their folder. A user-configured
    ffmpeg wins; otherwise we use the one the Windows archive ships with, which
    is why the whole archive is kept rather than just the executable.
    """
    suffix = ".exe" if os.name == "nt" else ""
    candidates: list[Path] = []
    if configured := ffmpeg_directory.strip():
        candidate = Path(configured).expanduser()
        candidates.append(candidate.parent if candidate.is_file() else candidate)
    candidates.append(executable.parent / "ffmpeg" / "bin")

    environment: dict[str, str] = {}
    for directory in candidates:
        ffmpeg = directory / f"ffmpeg{suffix}"
        ffprobe = directory / f"ffprobe{suffix}"
        if ffmpeg.is_file():
            environment["ALASS_FFMPEG_PATH"] = str(ffmpeg)
            if ffprobe.is_file():
                environment["ALASS_FFPROBE_PATH"] = str(ffprobe)
            break
    return environment
