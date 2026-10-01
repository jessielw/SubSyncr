from pathlib import Path
import tomllib

from subsyncr import __version__


def test_runtime_and_package_versions_agree():
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert (
        tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
        == __version__
    )
