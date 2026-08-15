"""Starting up: the first build, when the PDF is not there yet."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from texai.build import find_root_tex
from texai.cli import build_missing_pdf, main
from texai.config import AppConfig


def fake_build(tmp_path: Path, body: str) -> str:
    """A stand-in for latexmk: a script, since builds run without a shell."""
    script = tmp_path / "fake-build.sh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return shlex.quote(str(script))


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A project whose PDF has never been built."""
    (tmp_path / "main.tex").write_text("\\documentclass{article}\\begin{document}hi\\end{document}\n")
    return tmp_path


def config_for(root: Path, build_command: str | None = None) -> AppConfig:
    return AppConfig.create(root, root / "main.pdf", build_command=build_command)


def test_builds_the_pdf_when_it_is_missing(project: Path, capsys):
    config = config_for(project, fake_build(project, "printf '%%PDF-1.4\\n' > main.pdf"))
    assert not config.pdf_path.exists()

    assert build_missing_pdf(config) is None

    assert config.pdf_path.is_file()
    assert "is not there yet" in capsys.readouterr().out


def test_the_first_build_runs_where_the_root_tex_lives(tmp_path: Path):
    """The build's cwd is the .tex file's directory, not wherever texai started."""
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "main.tex").write_text("\\begin{document}hi\\end{document}\n")
    config = AppConfig.create(
        tmp_path,
        tmp_path / "paper" / "main.pdf",
        build_command=fake_build(tmp_path, "pwd > where && printf '%%PDF-1.4\\n' > main.pdf"),
    )

    assert build_missing_pdf(config) is None
    assert (tmp_path / "paper" / "main.pdf").is_file()
    assert (tmp_path / "paper" / "where").read_text().strip() == str(tmp_path / "paper")


def test_a_failed_first_build_is_reported_with_its_errors(project: Path):
    problem = build_missing_pdf(
        config_for(project, fake_build(project, "echo '! Undefined control sequence.'; exit 1"))
    )
    assert problem is not None
    assert "Build failed" in problem
    assert "Undefined control sequence" in problem


def test_a_build_that_writes_the_pdf_elsewhere_is_not_mistaken_for_success(project: Path):
    problem = build_missing_pdf(config_for(project, fake_build(project, "exit 0")))
    assert problem is not None
    assert "still not there" in problem


def test_a_missing_build_command_is_reported(project: Path):
    problem = build_missing_pdf(config_for(project, "/nonexistent/latexmk"))
    assert problem is not None
    assert "not found" in problem


def test_nothing_to_build_explains_itself(tmp_path: Path):
    """No .tex anywhere and no --build-cmd: say so, and say what to pass."""
    problem = build_missing_pdf(AppConfig.create(tmp_path, tmp_path / "main.pdf"))
    assert problem is not None
    assert "PDF not found" in problem
    assert "--build-cmd" in problem


def test_the_root_tex_is_the_one_named_after_the_pdf(tmp_path: Path):
    """The PDF need not exist for its source to be found — that is the point."""
    (tmp_path / "src").mkdir()
    root_tex = tmp_path / "src" / "main.tex"
    root_tex.write_text("\\begin{document}hi\\end{document}\n")
    (tmp_path / "notes.tex").write_text("\\begin{document}not this one\\end{document}\n")

    pdf = tmp_path / "build" / "main.pdf"
    assert find_root_tex(pdf, tmp_path) == root_tex

    config = AppConfig.create(tmp_path, pdf)
    assert config.root_tex == root_tex
    assert config.build_argv()[-1] == "main.tex"


def test_main_builds_and_then_serves(project: Path, monkeypatch):
    import uvicorn

    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    fake_build(project, "printf '%%PDF-1.4\\n' > main.pdf")

    code = main(
        [
            "--root",
            str(project),
            "--pdf",
            "main.pdf",
            "--build-cmd",
            f"{project / 'fake-build.sh'}",
        ]
    )
    assert code == 0
    assert (project / "main.pdf").is_file()
    assert served, "the server should start once the PDF is there"


def test_main_gives_up_when_the_first_build_fails(project: Path, monkeypatch, capsys):
    import uvicorn

    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(kwargs))
    fake_build(project, "echo '! Emergency stop.'; exit 1")

    code = main(
        [
            "--root",
            str(project),
            "--pdf",
            "main.pdf",
            "--build-cmd",
            f"{project / 'fake-build.sh'}",
        ]
    )
    assert code == 2
    assert not served, "a document that will not compile must not start a viewer"
    assert "Build failed" in capsys.readouterr().err
