import sys
from pathlib import Path

import pytest

from texai.build import (
    BuildError,
    default_build_command,
    find_root_tex,
    parse_log_errors,
    resolve_command,
    run_build,
)

LOG_WITH_ERROR = r"""
This is pdfTeX, Version 3.141592653
(./main.tex
LaTeX2e <2024-06-01>
! Undefined control sequence.
l.143 \nosuchmacro
                  {x}
?
! Emergency stop.
l.143 \nosuchmacro
"""


def test_parse_log_errors_extracts_message_and_line():
    errors = parse_log_errors(LOG_WITH_ERROR)
    assert errors[0].startswith("! Undefined control sequence.")
    assert "(line 143)" in errors[0]


def test_parse_log_errors_on_clean_log():
    assert parse_log_errors("Output written on main.pdf (1 page).\n") == []


def test_parse_log_errors_falls_back_to_latex_error_lines():
    errors = parse_log_errors("LaTeX Error: File `nope.sty' not found.\n")
    assert errors == ["LaTeX Error: File `nope.sty' not found."]


def test_parse_log_errors_is_capped():
    log = "\n".join(f"! Error number {i}." for i in range(50))
    assert len(parse_log_errors(log)) == 12


def test_find_root_tex_prefers_sibling(tmp_path: Path):
    (tmp_path / "build").mkdir()
    pdf = tmp_path / "build" / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    sibling = tmp_path / "build" / "main.tex"
    sibling.write_text("\\begin{document}\\end{document}\n")
    assert find_root_tex(pdf, tmp_path) == sibling


def test_find_root_tex_falls_back_to_begin_document(tmp_path: Path):
    (tmp_path / "build").mkdir()
    pdf = tmp_path / "build" / "out.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "chapter.tex").write_text("just an input\n")
    root = tmp_path / "paper.tex"
    root.write_text("\\documentclass{article}\\begin{document}hi\\end{document}\n")
    assert find_root_tex(pdf, tmp_path) == root


def test_find_root_tex_returns_none_when_nothing_matches(tmp_path: Path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    assert find_root_tex(pdf, tmp_path) is None


def test_default_build_command_enables_synctex(tmp_path: Path):
    argv = default_build_command(tmp_path / "main.tex")
    assert argv[0] == "latexmk"
    assert "-synctex=1" in argv
    assert argv[-1] == "main.tex"


def test_resolve_command_splits_without_a_shell():
    argv = resolve_command("latexmk -pdf 'my paper.tex'", None)
    assert argv == ["latexmk", "-pdf", "my paper.tex"]


def test_resolve_command_requires_something_to_run():
    with pytest.raises(BuildError, match="No build command"):
        resolve_command(None, None)
    with pytest.raises(BuildError, match="empty"):
        resolve_command("   ", None)


async def test_run_build_success(tmp_path: Path):
    result = await run_build([sys.executable, "-c", "print('ok')"], tmp_path)
    assert result.ok is True
    assert result.returncode == 0
    assert "ok" in result.output
    assert result.summary() == "Build succeeded."


async def test_run_build_failure_collects_errors(tmp_path: Path):
    script = "import sys; print('! Undefined control sequence.'); print('l.7 \\\\bad'); sys.exit(1)"
    result = await run_build([sys.executable, "-c", script], tmp_path)
    assert result.ok is False
    assert result.returncode == 1
    assert any("Undefined control sequence" in error for error in result.errors)
    assert "Build failed" in result.summary()


async def test_run_build_reads_the_log_when_stdout_is_quiet(tmp_path: Path):
    log = tmp_path / "main.log"
    log.write_text("! Missing $ inserted.\nl.12 x^2\n")
    result = await run_build([sys.executable, "-c", "import sys; sys.exit(1)"], tmp_path, log)
    assert any("Missing $ inserted" in error for error in result.errors)


async def test_run_build_missing_binary(tmp_path: Path):
    with pytest.raises(BuildError, match="not found"):
        await run_build(["texai-no-such-binary-xyz"], tmp_path)
