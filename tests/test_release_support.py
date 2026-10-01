"""Release notices must cover wheels that differ across target platforms."""

import hashlib
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


def test_supplemental_notice_provenance_hashes():
    folder = Path(__file__).resolve().parents[1] / "packaging/licenses"
    sources = json.loads((folder / "sources.json").read_text(encoding="utf-8"))
    for name, provenance in sources.items():
        assert (
            hashlib.sha256((folder / name).read_bytes()).hexdigest()
            == provenance["sha256"]
        ), name
