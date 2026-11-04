"""Inferring the project from one file: which .tex, which PDF, which root."""

from pathlib import Path

import pytest

from texai.discover import DiscoveryError, discover
from texai.paths import PathOutsideRootError

DOC = "\\documentclass{article}\n\\begin{document}hi\\end{document}\n"


@pytest.fixture()
def paper(tmp_path: Path) -> Path:
    root = (tmp_path / "paper").resolve()
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text(DOC)
    (root / "sections" / "model.tex").write_text("\\section{Model}\n")
    return root


def built(pdf: Path) -> Path:
    """A PDF as latexmk leaves it: with its SyncTeX data beside it."""
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    pdf.with_suffix(".synctex.gz").write_bytes(b"")
    return pdf


def test_a_tex_alone_is_enough(paper: Path):
    layout = discover(paper / "main.tex")
    assert layout.tex == paper / "main.tex"
    assert layout.pdf == paper / "main.pdf"  # need not exist: the first build makes it
    assert layout.root == paper


def test_a_relative_tex_is_read_from_the_shell(paper: Path, monkeypatch):
    monkeypatch.chdir(paper.parent)
    assert discover("paper/main.tex").tex == paper / "main.tex"


def test_a_pdf_alone_finds_the_tex_beside_it(paper: Path):
    layout = discover(paper / "main.pdf")
    assert (layout.tex, layout.root) == (paper / "main.tex", paper)


def test_a_pdf_in_a_build_directory_finds_the_tex_above_it(paper: Path):
    layout = discover(paper / "build" / "main.pdf")
    assert layout.tex == paper / "main.tex"
    assert layout.pdf == paper / "build" / "main.pdf"
    assert layout.root == paper  # holds both


def test_the_latexmkrc_out_dir_decides_where_the_pdf_goes(paper: Path):
    (paper / ".latexmkrc").write_text("$pdf_mode = 1;\n$out_dir = 'build';\n")
    assert discover(paper / "main.tex").pdf == paper / "build" / "main.pdf"


def test_a_pdf_already_built_below_the_tex_is_found(paper: Path):
    pdf = built(paper / "out" / "main.pdf")
    assert discover(paper / "main.tex").pdf == pdf


def test_a_pdf_beside_the_tex_wins_over_one_below(paper: Path):
    built(paper / "out" / "main.pdf")
    beside = built(paper / "main.pdf")
    assert discover(paper / "main.tex").pdf == beside


def test_a_directory_means_the_document_in_it(paper: Path):
    assert discover(paper).tex == paper / "main.tex"


def test_no_argument_means_the_current_directory(paper: Path, monkeypatch):
    monkeypatch.chdir(paper)
    assert discover().tex == paper / "main.tex"


def test_several_documents_prefer_main_and_otherwise_refuse_to_guess(paper: Path):
    (paper / "slides.tex").write_text(DOC)
    assert discover(paper).tex == paper / "main.tex"

    (paper / "main.tex").rename(paper / "paper.tex")
    with pytest.raises(DiscoveryError, match="several documents"):
        discover(paper)


def test_input_files_are_not_mistaken_for_documents(paper: Path):
    (paper / "main.tex").unlink()
    (paper / "macros.tex").write_text("% \\documentclass is not here\n\\newcommand\\x{}\n")
    with pytest.raises(DiscoveryError, match="no LaTeX document"):
        discover(paper)


def test_a_missing_tex_or_unknown_file_is_refused(paper: Path):
    with pytest.raises(DiscoveryError, match="no such file"):
        discover(paper / "nope.tex")
    (paper / "notes.md").write_text("x")
    with pytest.raises(DiscoveryError, match="expected a .tex or .pdf"):
        discover(paper / "notes.md")


def test_the_old_root_and_pdf_spelling_still_works(paper: Path, monkeypatch):
    monkeypatch.chdir(paper.parent)
    layout = discover(root=paper, pdf="main.pdf")  # relative to the root
    assert (layout.root, layout.pdf, layout.tex) == (paper, paper / "main.pdf", paper / "main.tex")


def test_overrides_win_over_inference(paper: Path):
    outer = paper.parent
    layout = discover(paper / "main.tex", root=outer, pdf=paper / "dist" / "paper.pdf")
    assert layout.root == outer
    assert layout.pdf == paper / "dist" / "paper.pdf"
    assert layout.tex == paper / "main.tex"


def test_a_tex_outside_an_explicit_root_is_refused(paper: Path):
    with pytest.raises(PathOutsideRootError):
        discover(paper / "main.tex", root=paper / "sections")
