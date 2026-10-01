from subsyncr import __version__
from subsyncr.core.diagnostics import support_summary


def test_support_summary_omits_private_paths(monkeypatch, tmp_path):
    private = str(tmp_path / "private-library")
    monkeypatch.setenv("SUBSYNCR_DATA_DIR", private)
    summary = support_summary()
    assert __version__ in summary
    assert "ffsubsync:" in summary
    assert "Python:" in summary
    assert private not in summary
