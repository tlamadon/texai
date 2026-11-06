import sys
from pathlib import Path

import pytest

from texai.build import (
    BuildError,
    default_build_command,
    detect_engine,
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


# ---------------------------------------------------------------- the engine


def test_a_plain_paper_builds_with_pdflatex(tmp_path: Path):
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}x\\end{document}\n")
    assert detect_engine(root) == ("pdflatex", "")
    assert resolve_command(None, root)[1] == "-pdf"


def test_fontspec_in_a_class_several_directories_away_needs_lualatex(tmp_path: Path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "letter.cls").write_text(
        "\\LoadClass{article}\n\\RequirePackage{fontspec}\n\\setmainfont{TeX Gyre Pagella}\n"
    )
    folder = tmp_path / "letters" / "one"
    folder.mkdir(parents=True)
    root = folder / "letter.tex"
    root.write_text("\\documentclass{../../templates/letter}\n\\begin{document}x\\end{document}\n")

    engine, why = detect_engine(root)
    assert engine == "lualatex"
    assert "letter.cls" in why and "fontspec" in why
    assert resolve_command(None, root)[1] == "-lualatex"


def test_an_input_preamble_is_followed(tmp_path: Path):
    (tmp_path / "preamble.tex").write_text("\\usepackage[T1]{fontenc}\n\\usepackage{xeCJK}\n")
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n\\input{preamble}\n\\begin{document}x\\end{document}\n")
    assert detect_engine(root)[0] == "xelatex"


def test_a_lua_only_package_beats_a_generic_unicode_one(tmp_path: Path):
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n\\usepackage{fontspec,luacode}\n")
    assert detect_engine(root)[0] == "lualatex"


def test_commented_out_packages_do_not_count(tmp_path: Path):
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n% \\usepackage{fontspec}\n\\usepackage{amsmath} % not fontspec\n")
    assert detect_engine(root)[0] == "pdflatex"


def test_a_magic_comment_wins(tmp_path: Path):
    root = tmp_path / "main.tex"
    root.write_text("% !TEX program = xelatex\n\\documentclass{article}\n\\usepackage{fontspec}\n")
    assert detect_engine(root) == ("xelatex", "main.tex asks for xelatex")


def test_latexmkrc_pdf_mode_is_honoured(tmp_path: Path):
    (tmp_path / ".latexmkrc").write_text("$pdf_mode = 5;\n")
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n")
    assert detect_engine(root)[0] == "xelatex"


def test_an_explicit_build_command_is_left_alone(tmp_path: Path):
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n\\usepackage{fontspec}\n")
    assert resolve_command("latexmk -pdf main.tex", root) == ["latexmk", "-pdf", "main.tex"]
