from pathlib import Path

import pytest

from texai.config import AppConfig
from texai.navigate import LocateError, locate
from texai.synctex import SyncTexBox, SyncTexError

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n")
    (root / "sections" / "model.tex").write_text("alpha\nbeta\n")
    (root / "main.pdf").write_bytes(PDF_BYTES)
    (root / "main.synctex.gz").write_bytes(b"\x1f\x8b")
    return root.resolve()


@pytest.fixture()
def config(project: Path) -> AppConfig:
    return AppConfig.create(project, project / "main.pdf")


def fake_view(boxes):
    def runner(pdf_path, source, line, column=1, root=None, executable="synctex", timeout=15.0):
        return boxes

    return runner


def test_locate_returns_page_and_boxes(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "texai.navigate.run_synctex_view",
        fake_view([SyncTexBox(page=4, x=72.0, y=99.1, width=468.0, height=10.9)]),
    )
    found = locate(config, "sections/model.tex", 2)
    assert found["found"] is True
    assert found["page"] == 4
    assert found["file"] == "sections/model.tex"
    assert found["line"] == 2
    assert found["boxes"][0]["width"] == 468.0


def test_locate_reports_lines_that_render_nothing(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    """A comment or an untracked environment is a normal answer, not an error."""
    monkeypatch.setattr("texai.navigate.run_synctex_view", fake_view([]))
    found = locate(config, "sections/model.tex", 1)
    assert found == {
        "found": False,
        "file": "sections/model.tex",
        "line": 1,
        "page": None,
        "boxes": [],
    }


def test_locate_clamps_nonsense_line_numbers(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    seen = {}

    def runner(pdf_path, source, line, **kwargs):
        seen["line"] = line
        return []

    monkeypatch.setattr("texai.navigate.run_synctex_view", runner)
    locate(config, "sections/model.tex", -5)
    assert seen["line"] == 1


def test_locate_refuses_paths_outside_the_root(config: AppConfig):
    with pytest.raises(LocateError, match="outside the project root"):
        locate(config, "../../../etc/passwd", 1)


def test_locate_refuses_absolute_paths_outside_the_root(config: AppConfig):
    with pytest.raises(LocateError, match="outside the project root"):
        locate(config, "/etc/passwd", 1)


def test_locate_reports_a_missing_file(config: AppConfig):
    with pytest.raises(LocateError, match="No such file"):
        locate(config, "sections/ghost.tex", 1)


def test_locate_surfaces_synctex_failures(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise SyncTexError("synctex exploded")

    monkeypatch.setattr("texai.navigate.run_synctex_view", boom)
    with pytest.raises(LocateError, match="synctex exploded"):
        locate(config, "sections/model.tex", 1)


def test_locate_normalises_the_returned_path(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    """An absolute in-root path comes back project-relative, ready for the UI."""
    monkeypatch.setattr(
        "texai.navigate.run_synctex_view",
        fake_view([SyncTexBox(page=1, x=1, y=2, width=3, height=4)]),
    )
    absolute = str(config.root / "sections" / "model.tex")
    assert locate(config, absolute, 2)["file"] == "sections/model.tex"
