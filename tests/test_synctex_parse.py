from pathlib import Path

import pytest

from texai.synctex import (
    SyncTexDataMissing,
    SyncTexExecutableMissing,
    SyncTexNoResult,
    parse_synctex_edit,
    run_synctex_edit,
    synctex_data_file,
)

TYPICAL = """This is SyncTeX command line utility, version 1.5
SyncTeX result begin
Output:build/main.pdf
Input:/home/me/project/sections/model.tex
Line:143
Column:-1
Offset:0
Context:
SyncTeX result end
"""


def test_parses_typical_output():
    location = parse_synctex_edit(TYPICAL)
    assert location.input == "/home/me/project/sections/model.tex"
    assert location.line == 143
    # Column:-1 means "unknown"; normalised to 1.
    assert location.column == 1


def test_parses_real_column():
    location = parse_synctex_edit(TYPICAL.replace("Column:-1", "Column:12"))
    assert location.column == 12


def test_relative_input_and_crlf():
    output = "SyncTeX result begin\r\nInput:./sections/model.tex\r\nLine:7\r\nSyncTeX result end\r\n"
    location = parse_synctex_edit(output)
    assert location.input == "./sections/model.tex"
    assert location.line == 7
    assert location.column == 1


def test_windows_style_absolute_input_keeps_drive_letter():
    output = "SyncTeX result begin\nInput:C:/tex/main.tex\nLine:3\nSyncTeX result end\n"
    assert parse_synctex_edit(output).input == "C:/tex/main.tex"


def test_first_complete_record_wins():
    output = (
        "SyncTeX result begin\n"
        "Input:sections/a.tex\nLine:10\nColumn:-1\n"
        "Input:sections/b.tex\nLine:20\nColumn:-1\n"
        "SyncTeX result end\n"
    )
    location = parse_synctex_edit(output)
    assert (location.input, location.line) == ("sections/a.tex", 10)


def test_line_zero_is_clamped_to_one():
    output = "SyncTeX result begin\nInput:main.tex\nLine:0\nSyncTeX result end\n"
    assert parse_synctex_edit(output).line == 1


@pytest.mark.parametrize(
    "output",
    [
        "",
        "This is SyncTeX command line utility, version 1.5\n",
        "SyncTeX result begin\nSyncTeX result end\n",
        "SyncTeX result begin\nInput:main.tex\nSyncTeX result end\n",  # no Line
        "SyncTeX result begin\nInput:main.tex\nLine:abc\nSyncTeX result end\n",
        "SyncTeX result begin\nInput:\nLine:5\nSyncTeX result end\n",  # empty Input
        "SyncTeX Warning: No file /tmp/nope.synctex.gz\n",
    ],
)
def test_unusable_output_returns_none(output):
    assert parse_synctex_edit(output) is None


def test_bad_column_falls_back_to_one():
    output = "SyncTeX result begin\nInput:main.tex\nLine:5\nColumn:xx\nSyncTeX result end\n"
    assert parse_synctex_edit(output).column == 1


def test_synctex_data_file_prefers_gz(tmp_path: Path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    assert synctex_data_file(pdf) is None

    plain = tmp_path / "main.synctex"
    plain.write_text("x")
    assert synctex_data_file(pdf) == plain

    gz = tmp_path / "main.synctex.gz"
    gz.write_bytes(b"\x1f\x8b")
    assert synctex_data_file(pdf) == gz


def test_missing_pdf_raises(tmp_path: Path):
    with pytest.raises(SyncTexDataMissing, match="PDF not found"):
        run_synctex_edit(tmp_path / "nope.pdf", 1, 100, 100)


def test_missing_synctex_data_raises(tmp_path: Path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(SyncTexDataMissing, match="No SyncTeX data"):
        run_synctex_edit(pdf, 1, 100, 100)


def test_missing_executable_raises(tmp_path: Path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "main.synctex.gz").write_bytes(b"\x1f\x8b")
    with pytest.raises(SyncTexExecutableMissing, match="not found"):
        run_synctex_edit(pdf, 1, 100, 100, executable="synctex-does-not-exist-xyz")


def test_no_result_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "main.synctex.gz").write_bytes(b"\x1f\x8b")

    class Completed:
        stdout = "SyncTeX Warning: No tag for page 1\n"
        stderr = ""

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return Completed()

    monkeypatch.setattr("texai.synctex.subprocess.run", fake_run)
    with pytest.raises(SyncTexNoResult):
        run_synctex_edit(pdf, 3, 241.3, 418.2)

    # Never through a shell, and the query is a single argv element.
    assert captured["shell"] is False
    assert captured["argv"][:3] == ["synctex", "edit", "-o"]
    assert captured["argv"][3] == f"3:241.300:418.200:{pdf}"


def test_shell_metacharacters_stay_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A PDF path with shell syntax must land in argv verbatim, unexpanded."""
    weird = tmp_path / "a b; rm -rf $HOME.pdf"
    weird.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "a b; rm -rf $HOME.synctex.gz").write_bytes(b"\x1f\x8b")

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class C:
            stdout = "SyncTeX result begin\nInput:main.tex\nLine:1\nSyncTeX result end\n"
            stderr = ""

        return C()

    monkeypatch.setattr("texai.synctex.subprocess.run", fake_run)
    run_synctex_edit(weird, 1, 0, 0)
    assert captured["argv"][3].endswith(str(weird))
