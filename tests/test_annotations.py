"""The ``%@`` annotations: what counts as one, and where it lands on the page.

The placement half runs against a hand-built SyncTeX line index rather than a
real build, so it pins the rules — own paragraph, else the next one down; an
intent at the paragraph's top, a note at the line it precedes — without TeX.
"""

from pathlib import Path

from texai.annotations import Annotation, parse_annotations
from texai.paragraphs import _map_file


def test_kinds_text_and_lines():
    text = (
        "%@intent motivation, contrast\n"  # 1
        "Body.\n"  # 2
        "  %@todo: cite Smith\n"  # 3
        "%@ a bare one\n"  # 4
        "%@Comment Mixed case kind\n"  # 5
    )
    assert parse_annotations(text) == [
        Annotation(1, "intent", "motivation, contrast"),
        Annotation(3, "todo", "cite Smith"),
        Annotation(4, "comment", "a bare one"),
        Annotation(5, "comment", "Mixed case kind"),
    ]


def test_ordinary_comments_and_escaped_percents_are_not_annotations():
    text = "% a plain comment\n% @not one\n50\\%@ off\nText % @trailing\n%@\n%@intent   \n"
    assert parse_annotations(text) == []


def test_a_space_after_the_at_makes_it_a_comment_not_a_kind():
    assert parse_annotations("%@ Fix this") == [Annotation(1, "comment", "Fix this")]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "s.tex"
    path.write_text(text)
    return path


def _at(page: int, y: float) -> dict[int, list[float]]:
    return {page: [y, y]}


def test_intent_sits_at_the_paragraph_top_and_a_note_beside_its_line(tmp_path):
    path = _write(
        tmp_path,
        "%@intent setup\n"  # 1
        "First line.\n"  # 2
        "%@comment about line four\n"  # 3
        "Second line.\n",  # 4
    )
    index = {("s.tex", 2): _at(1, 100.0), ("s.tex", 4): _at(1, 112.0)}
    bars, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    assert len(bars) == 1
    intent, comment = notes
    assert (intent["kind"], intent["page"], intent["index"]) == ("intent", 1, 0)
    assert intent["y"] == bars[0]["y"]  # level with the top of the bar
    assert comment["kind"] == "comment"
    assert comment["y"] == round(112.0 - 9.0, 2)  # the line after it, not the top
    assert (comment["x"], comment["right"]) == (72.0, 540.0)


def test_a_block_of_annotations_belongs_to_the_next_paragraph(tmp_path):
    path = _write(
        tmp_path,
        "%@intent result\n"  # 1  paragraph 0: annotations only
        "\n"  # 2
        "% a plain comment block\n"  # 3  paragraph 1: renders nothing either
        "\n"  # 4
        "The result.\n",  # 5  paragraph 2
    )
    index = {("s.tex", 5): _at(2, 300.0)}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    assert [(n["index"], n["page"], n["palette"]) for n in notes] == [(2, 2, 2)]


def test_a_note_after_the_last_line_falls_back_to_the_line_before(tmp_path):
    path = _write(tmp_path, "Only line.\n%@todo trailing\n")
    index = {("s.tex", 1): _at(3, 200.0)}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    assert [(n["page"], n["y"]) for n in notes] == [(3, round(200.0 - 9.0, 2))]


def test_the_paragraph_start_follows_source_order_not_the_earliest_page(tmp_path):
    # A page shipped out mid-paragraph tags its footer with whatever line TeX
    # was reading, so a later line can claim an earlier page. The intent still
    # belongs where the paragraph's first line typeset.
    path = _write(tmp_path, "%@intent x\nFirst.\nSecond.\n")
    index = {("s.tex", 2): _at(2, 100.0), ("s.tex", 3): {1: [750.0, 750.0], 2: [112.0, 112.0]}}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    assert [(n["page"], n["y"]) for n in notes] == [(2, round(100.0 - 9.0, 2))]


def test_preamble_annotations_and_unrendered_tails_are_dropped(tmp_path):
    path = _write(
        tmp_path,
        "%@todo in the preamble\n"  # 1
        "\\begin{document}\n"  # 2
        "Body.\n"  # 3
        "\n"  # 4
        "%@todo nothing below me renders\n",  # 5
    )
    index = {("s.tex", 3): _at(1, 100.0)}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    assert notes == []
