"""Release notices must cover wheels that differ across target platforms."""

import importlib
from importlib.metadata import PathDistribution
import json
from pathlib import Path
import shutil

import pytest


@pytest.fixture
def notice_build(tmp_path, monkeypatch):
    hooks = pytest.importorskip("PyInstaller.utils.hooks")
    project = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(project / "packaging"))
    support = importlib.import_module("release_support")
    (tmp_path / "build").mkdir()
    shutil.copytree(project / "packaging/licenses", tmp_path / "packaging/licenses")
    for name in ("LICENSE", "README.md", "CHANGELOG.md", "THIRD_PARTY.md"):
        (tmp_path / name).write_text("test document", encoding="utf-8")

    # The generic 0.9.0.post1 wheel has metadata but no LICENSE or COPYING.
    metadata = tmp_path / "site-packages/PySoundFile-0.9.0.post1.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.0\nName: PySoundFile\nVersion: 0.9.0.post1\n",
        encoding="utf-8",
    )
    (metadata / "RECORD").write_text(
        "soundfile.py,,\nPySoundFile-0.9.0.post1.dist-info/METADATA,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        support, "packages_distributions", lambda: {"soundfile": ["PySoundFile"]}
    )
    monkeypatch.setattr(
        support, "distribution", lambda name: PathDistribution(metadata)
    )
    monkeypatch.setattr(
        hooks, "copy_metadata", lambda name: [(str(metadata), metadata.name)]
    )
    return support, tmp_path


def test_generic_pysoundfile_wheel_gets_upstream_notice(notice_build):
    support, root = notice_build
    entries = support.collect_notices(root, ["soundfile"])
    bundled = {target.replace("\\", "/"): Path(source) for target, source, _ in entries}
    inventory = json.loads(bundled["THIRD_PARTY_INVENTORY.json"].read_text())
    assert len(inventory) == 1
    assert inventory[0]["name"] == "PySoundFile"
    assert inventory[0]["version"] == "0.9.0.post1"
    assert inventory[0]["license_files"] == ["licenses/PySoundFile-LICENSE.txt"]
    for target in inventory[0]["license_files"]:
        assert bundled[target].is_file()
        assert "Copyright (c) 2013, Bastian Bechtold" in bundled[target].read_text()


def test_missing_supplemental_notice_still_fails(notice_build):
    support, root = notice_build
    (root / "packaging/licenses/PySoundFile-LICENSE.txt").unlink()
    with pytest.raises(FileNotFoundError, match="Missing supplemental license"):
        support.collect_notices(root, ["soundfile"])


def test_unknown_package_without_notice_still_fails(notice_build, monkeypatch):
    support, root = notice_build
    monkeypatch.setattr(
        support, "packages_distributions", lambda: {"unknown": ["unknown"]}
    )
    with pytest.raises(ValueError, match="No license file collected for unknown"):
        support.collect_notices(root, ["unknown"])


@pytest.mark.parametrize(
    ("version", "numbers"),
    [
        ("0.1.0", (0, 1, 0)),
        ("0.1.0-dev.1", (0, 1, 0)),
        ("1.2.3rc1", (1, 2, 3)),
        ("10.20.30.post2", (10, 20, 30)),
    ],
)
def test_prerelease_versions_get_numeric_release(monkeypatch, version, numbers):
    project = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(project / "packaging"))
    support = importlib.import_module("release_support")
    assert support.numeric_version(version) == numbers


def test_windows_resource_keeps_prerelease_label(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(project / "packaging"))
    support = importlib.import_module("release_support")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.0-dev.1"\n', encoding="utf-8"
    )
    resource = support.windows_version(tmp_path, "0.1.0-dev.1").read_text()
    assert "filevers=(0, 1, 0, 0)" in resource
    assert "StringStruct('ProductVersion', '0.1.0-dev.1')" in resource
