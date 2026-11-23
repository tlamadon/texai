"""`texai doctor`: what is missing from the machine, and how to get it."""

from __future__ import annotations

from pathlib import Path

import pytest

from texai import doctor
from texai.cli import main


def only(monkeypatch, *present: str) -> None:
    """A machine with just these tools on PATH."""
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: f"/bin/{name}" if name in present else None
    )
    monkeypatch.setattr(doctor, "_version", lambda *args: "1.0")


def test_a_complete_machine_passes(monkeypatch, capsys):
    only(monkeypatch, "latexmk", "pdflatex", "synctex", "claude", "git", "perl")
    assert doctor.run() == 0
    out = capsys.readouterr().out
    assert "Everything texai needs is here" in out
    assert "MISSING" not in out


@pytest.mark.parametrize(
    ("name", "said"),
    [("Darwin", "brew install --cask mactex-no-gui"), ("Linux", "apt install"), ("Windows", "MiKTeX")],
)
def test_missing_tex_comes_with_this_systems_remedy(monkeypatch, capsys, name, said):
    only(monkeypatch, "claude", "git", "perl")
    monkeypatch.setattr(doctor.platform, "system", lambda: name)
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "MISSING  latexmk" in out and said in out
    assert "Missing: latexmk, pdflatex, synctex." in out


def test_windows_is_also_asked_for_perl(monkeypatch):
    only(monkeypatch, "latexmk", "pdflatex", "synctex", "claude", "git")
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    (perl,) = [check for check in doctor.checks() if not check.ok]
    assert perl.name == "perl" and "StrawberryPerl" in perl.fix[0]
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    assert all(check.ok for check in doctor.checks())


def test_git_is_optional(monkeypatch, capsys):
    only(monkeypatch, "latexmk", "pdflatex", "synctex", "claude")
    assert doctor.run() == 0
    assert "(optional)" in capsys.readouterr().out


def test_a_missing_claude_says_how_to_sign_in(monkeypatch, capsys):
    only(monkeypatch, "latexmk", "pdflatex", "synctex", "git")
    assert doctor.run() == 1
    out = capsys.readouterr().out
    assert "MISSING  claude" in out and "sign in" in out


def test_texai_doctor_is_the_command(monkeypatch, tmp_path: Path, capsys):
    only(monkeypatch, "latexmk", "pdflatex", "synctex", "claude", "git")
    monkeypatch.chdir(tmp_path)
    assert main(["doctor"]) == 0
    assert "Everything texai needs is here" in capsys.readouterr().out


def test_a_first_run_without_tex_lists_everything_missing(monkeypatch, tmp_path: Path, capsys):
    """Not one error for the first missing tool: all of them, with the way out."""
    only(monkeypatch, "git")
    (tmp_path / "main.tex").write_text("\\documentclass{article}\n\\begin{document}x\\end{document}\n")
    assert main([str(tmp_path / "main.tex")]) == 2
    err = capsys.readouterr().err
    assert "there is no TeX here" in err
    for name in ("latexmk", "synctex", "claude"):
        assert f"MISSING  {name}" in err
