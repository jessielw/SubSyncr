"""The architectural invariant, asserted rather than trusted.

The whole design rests on ``core`` and ``cli`` being frontend-agnostic. That is
easy to state and easy to violate one convenient import at a time, so it is
checked here instead of in a style guide.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "ffsubsyncui"
_UI_FREE_PACKAGES = ("core", "cli")


def _modules(package: str) -> list[Path]:
    return sorted((_SOURCE_ROOT / package).rglob("*.py"))


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("package", _UI_FREE_PACKAGES)
def test_package_has_modules(package: str) -> None:
    assert _modules(package), f"no modules found under {package}"


@pytest.mark.parametrize("package", _UI_FREE_PACKAGES)
def test_no_qt_imports(package: str) -> None:
    offenders = {
        path.relative_to(_SOURCE_ROOT).as_posix(): sorted(
            name for name in _imported_names(path) if name.split(".")[0] == "PySide6"
        )
        for path in _modules(package)
    }
    leaked = {module: names for module, names in offenders.items() if names}
    assert not leaked, f"Qt leaked into UI-free packages: {leaked}"


def test_core_does_not_import_cli_or_qt() -> None:
    """core sits at the bottom; nothing above it may be a dependency."""
    leaked: dict[str, list[str]] = {}
    for path in _modules("core"):
        names = sorted(
            name
            for name in _imported_names(path)
            if name.startswith(("ffsubsyncui.qt", "ffsubsyncui.cli"))
        )
        if names:
            leaked[path.relative_to(_SOURCE_ROOT).as_posix()] = names
    assert not leaked, f"core reached upwards: {leaked}"


def test_worker_avoids_qt_at_import_time() -> None:
    """The frozen executable re-execs itself as the worker.

    If importing the worker dragged in Qt, every sync would pay to start a GUI
    toolkit it never uses.
    """
    assert "PySide6" not in _imported_names(_SOURCE_ROOT / "core" / "worker.py")
