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


# ------------------------------------------------------------- locate_range

from texai.navigate import MAX_RANGE_LINES, locate_range  # noqa: E402


def box(page=1, y=100.0):
    return SyncTexBox(page=page, x=72.0, y=y, width=468.0, height=10.0)


def test_locate_range_covers_every_line_of_the_span(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    """A multi-line change must not be highlighted from its first line only."""
    per_line = {1: [box(y=10)], 2: [box(y=20)], 3: [box(y=30)]}
    asked: list[int] = []

    def runner(pdf_path, source, line, **kwargs):
        asked.append(line)
        return per_line.get(line, [])

    monkeypatch.setattr("texai.navigate.run_synctex_view", runner)
    found = locate_range(config, "sections/model.tex", 1, 3)

    assert asked == [1, 2, 3]
    assert [b["y"] for b in found["boxes"]] == [10.0, 20.0, 30.0]
    assert found["found"] is True


def test_locate_range_dedupes_shared_render_lines(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    """Consecutive source lines usually share a rendered line; report it once."""
    monkeypatch.setattr("texai.navigate.run_synctex_view", lambda *a, **k: [box(y=50)])
    found = locate_range(config, "sections/model.tex", 1, 5)
    assert len(found["boxes"]) == 1


def test_locate_range_sorts_by_page_then_position(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    out = {1: [box(page=2, y=10)], 2: [box(page=1, y=90)], 3: [box(page=1, y=20)]}
    monkeypatch.setattr("texai.navigate.run_synctex_view", lambda p, s, line, **k: out[line])
    boxes = locate_range(config, "sections/model.tex", 1, 3)["boxes"]
    assert [(b["page"], b["y"]) for b in boxes] == [(1, 20.0), (1, 90.0), (2, 10.0)]


def test_locate_range_survives_an_unmappable_line(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    def runner(pdf_path, source, line, **kwargs):
        if line == 2:
            raise SyncTexError("no record for that line")
        return [box(y=line * 10)]

    monkeypatch.setattr("texai.navigate.run_synctex_view", runner)
    found = locate_range(config, "sections/model.tex", 1, 3)
    assert [b["y"] for b in found["boxes"]] == [10.0, 30.0]


def test_locate_range_is_capped(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    asked: list[int] = []

    def runner(pdf_path, source, line, **kwargs):
        asked.append(line)
        return []

    monkeypatch.setattr("texai.navigate.run_synctex_view", runner)
    found = locate_range(config, "sections/model.tex", 1, 5000)
    assert len(asked) == MAX_RANGE_LINES
    assert found["truncated"] is True


def test_locate_range_handles_a_single_line(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("texai.navigate.run_synctex_view", lambda *a, **k: [box()])
    found = locate_range(config, "sections/model.tex", 7, 7)
    assert found["line"] == 7 and found["lastLine"] == 7
    assert len(found["boxes"]) == 1


def test_locate_range_refuses_paths_outside_the_root(config: AppConfig):
    with pytest.raises(LocateError, match="outside the project root"):
        locate_range(config, "../../etc/passwd", 1, 3)


# -------------------------------------------------- against real synctex output

EXAMPLE = Path(__file__).resolve().parents[1] / "example"


@pytest.mark.skipif(
    not (EXAMPLE / "main.pdf").is_file() or not (EXAMPLE / "main.synctex.gz").is_file(),
    reason="the example has not been compiled (latexmk -pdf -synctex=1 main.tex)",
)
def test_multi_line_span_covers_more_than_its_first_line():
    """The real regression: lines 8-10 of a wrapped paragraph render on two lines."""
    real = AppConfig.create(EXAMPLE, EXAMPLE / "main.pdf")
    first_only = locate(real, "sections/model.tex", 8)["boxes"]
    whole_span = locate_range(real, "sections/model.tex", 8, 10)["boxes"]
    assert len(whole_span) > len(first_only)
    assert {b["y"] for b in first_only} < {b["y"] for b in whole_span}
