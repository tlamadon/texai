"""Reading the paper's own macros, for rendering math in replies."""

from pathlib import Path

from texai.config import AppConfig
from texai.macros import document_macros

PREAMBLE = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage{notation}
\input{extra}
\newcommand{\E}{\mathbb{E}}
\newcommand\R{\mathbb{R}}
\renewcommand*{\vec}[1]{\boldsymbol{#1}}
\newcommand{\opt}[2][x]{#1 + #2}
\def\half{\frac{1}{2}}
\def\pair#1#2{(#1, #2)}
\DeclareMathOperator{\Var}{Var}
\DeclareMathOperator*{\argmax}{arg\,max}
% \newcommand{\commented}{nope}
\begin{document}
\newcommand{\late}{too late}
\end{document}
"""


def make(tmp_path: Path) -> AppConfig:
    (tmp_path / "main.tex").write_text(PREAMBLE)
    (tmp_path / "notation.sty").write_text("\\providecommand{\\N}{\\mathbb{N}}\n")
    (tmp_path / "extra.tex").write_text("\\newcommand{\\set}[1]{\\{#1\\}}\n")
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return AppConfig.create(tmp_path, tmp_path / "main.pdf")


def test_simple_definitions_are_read_from_the_preamble_and_local_files(tmp_path: Path):
    macros = document_macros(make(tmp_path))
    assert macros["\\E"] == "\\mathbb{E}"
    assert macros["\\R"] == "\\mathbb{R}"
    assert macros["\\vec"] == "\\boldsymbol{#1}"
    assert macros["\\half"] == "\\frac{1}{2}"
    assert macros["\\pair"] == "(#1, #2)"
    assert macros["\\Var"] == "\\operatorname{Var}"
    assert macros["\\argmax"] == "\\operatorname*{arg\\,max}"
    # From a local package and an \input file, as TeX would find them.
    assert macros["\\N"] == "\\mathbb{N}"
    assert macros["\\set"] == "\\{#1\\}"  # escaped braces do not end the body


def test_what_cannot_be_translated_is_left_out(tmp_path: Path):
    macros = document_macros(make(tmp_path))
    assert "\\opt" not in macros  # an optional argument
    assert "\\commented" not in macros
    assert "\\late" not in macros  # defined after \begin{document}


def test_no_root_file_means_no_macros(tmp_path: Path):
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert document_macros(AppConfig.create(tmp_path, tmp_path / "main.pdf")) == {}
