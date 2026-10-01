"""Build-time version resources and licenses for the actual collected modules."""

from __future__ import annotations

from importlib.metadata import distribution, packages_distributions
import json
from pathlib import Path
import re
import tempfile
import tomllib


def collect_notices(root: Path, modules: list[str]) -> list[tuple[str, str, str]]:
    """Return PyInstaller TOC entries, including metadata for support diagnostics.

    Select installed distributions by modules actually collected by Analysis, so
    build/test-only dependencies do not appear as shipped software. Wheel license
    files are copied alongside a versioned inventory; supplemental font and Qt
    notices are maintained under packaging/licenses with upstream provenance.
    """
    from PyInstaller.utils.hooks import copy_metadata

    folder = Path(tempfile.mkdtemp(prefix="release-notices-", dir=root / "build"))
    mapping = packages_distributions()
    names = {
        name for module in modules for name in mapping.get(module.split(".")[0], [])
    }
    inventory = []
    entries: list[tuple[str, str, str]] = []
    for name in sorted(names):
        package = distribution(name)
        files = []
        for file in package.files or ():
            # Keep the original relative path to avoid collisions among bundled
            # libraries' notices, but never copy metadata paths escaping a wheel.
            relative = Path(str(file))
            if ".." in relative.parts or relative.is_absolute():
                continue
            if not any(
                word in relative.name.lower()
                for word in ("license", "licence", "copying", "notice", "copyright")
            ):
                continue
            source = Path(str(package.locate_file(file)))
            if source.is_file():
                destination = Path("licenses") / name / relative
                entries.append((str(destination), str(source), "DATA"))
                files.append(str(destination).replace("\\", "/"))
        supplemental = {
            "faust-cchardet": ["faust-cchardet-COPYING.txt"],
            # The generic wheel used on Linux/macOS ARM omits its own license.
            "pysoundfile": ["PySoundFile-LICENSE.txt"],
            "pyside6-essentials": [
                "LGPL-3.0-only.txt",
                "GPL-3.0-only.txt",
                "Qt-third-party.html",
            ],
            "shiboken6": ["LGPL-3.0-only.txt", "GPL-3.0-only.txt"],
        }.get(name.lower().replace("_", "-"), [])
        for notice in supplemental:
            if not (root / "packaging" / "licenses" / notice).is_file():
                raise FileNotFoundError(f"Missing supplemental license: {notice}")
            files.append(f"licenses/{notice}")
        if not files:
            raise ValueError(
                f"No license file collected for {name}; add its upstream notice"
            )
        inventory.append(
            {
                "name": name,
                "version": package.version,
                "license": package.metadata.get("License-Expression")
                or package.metadata.get("License", "See upstream notices"),
                "homepage": package.metadata.get("Home-page", ""),
                "project_urls": package.metadata.get_all("Project-URL") or [],
                "license_files": files,
            }
        )
        for source, destination in copy_metadata(name):
            source_path = Path(source)
            for file in source_path.rglob("*"):
                if file.is_file() and file.name != "direct_url.json":
                    target = Path(destination) / file.relative_to(source_path)
                    entries.append((str(target), str(file), "DATA"))
    manifest = folder / "THIRD_PARTY_INVENTORY.json"
    manifest.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    entries.append((manifest.name, str(manifest), "DATA"))
    for name in ("LICENSE", "README.md", "CHANGELOG.md", "THIRD_PARTY.md"):
        entries.append((name, str(root / name), "DATA"))
    for file in (root / "packaging" / "licenses").iterdir():
        if file.is_file():
            entries.append((str(Path("licenses") / file.name), str(file), "DATA"))
    for file in (root / "docs").rglob("*"):
        if file.is_file():
            entries.append((str(file.relative_to(root)), str(file), "DATA"))
    return entries


def numeric_version(version: str) -> tuple[int, int, int]:
    """The X.Y.Z release a version string starts with, e.g. 0.1.0-dev.1 -> 0.1.0.

    Windows version resources and macOS bundle versions only accept integers,
    so pre-release suffixes are dropped there and kept in the display strings.
    """
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise ValueError(f"Version must start with X.Y.Z: {version}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def windows_version(root: Path, version: str) -> Path:
    """Generate Explorer's version resource from the checked runtime version."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if project["project"]["version"] != version:
        raise ValueError("Package and runtime versions disagree")
    numbers = (*numeric_version(version), 0)
    resource = root / "build" / "windows-version.txt"
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, mask=0x3f,
    flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'jessielw'),
    StringStruct('FileDescription', 'SubSyncr subtitle synchronization'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'SubSyncr'),
    StringStruct('OriginalFilename', 'SubSyncr.exe'),
    StringStruct('ProductName', 'SubSyncr'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""",
        encoding="utf-8",
    )
    return resource
