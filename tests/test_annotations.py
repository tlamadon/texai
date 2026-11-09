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


# ---------------------------------------------------------------- the grammar


def test_generic_fields_come_first_and_the_rest_is_text_as_written():
    (note,) = parse_annotations('%@comment id:c4 by:"Thibaut L" re:c3 status:done Note: see -- TL')
    assert (note.id, note.by, note.re, note.status) == ("c4", "Thibaut L", "c3", "done")
    assert note.text == "Note: see -- TL"


def test_a_double_dash_after_the_fields_starts_the_text():
    (note,) = parse_annotations("%@todo id:t2 status:done -- cite the replication")
    assert (note.id, note.status, note.text) == ("t2", "done", "cite the replication")


def test_unknown_fields_are_text_not_fields():
    (note,) = parse_annotations("%@comment see http://example.org first")
    assert note.text == "see http://example.org first" and not note.fields


def test_continuation_lines_carry_an_annotation_on():
    text = "%@comment id:c1 A longer thought\n%> that runs on\n  %>   indented\nBody.\n%> stray\n"
    (note,) = parse_annotations(text)
    assert (note.line, note.end) == (1, 3)
    assert note.text == "A longer thought\nthat runs on\n  indented"
    assert note.payload == ("that runs on", "  indented")


def test_kinds_say_where_they_go():
    found = parse_annotations(
        "%@intent why\n%@question odd?\n%@src @k p.3\n%@propose -- v\n%> x\nLine.\n"
    )
    assert [a.place for a in found] == ["intent", "note", "marker", "proposal"]
    src = found[2]
    assert (src.args, dict(src.fields)) == (("@k",), {"p": "3"})
    assert found[1].as_dict()["place"] == "note"


def test_page_shorthand_is_only_for_kinds_that_take_a_page():
    (note,) = parse_annotations("%@comment p.3 is wrong\n")
    assert note.text == "p.3 is wrong" and not note.fields


def test_a_proposal_is_placed_beside_the_line_it_replaces(tmp_path):
    path = _write(
        tmp_path,
        "First line.\n"  # 1
        "%@propose id:p1 by:agent -- shorter\n"  # 2
        "%> Short.\n"  # 3
        "%@propose id:p1 -- longer\n"  # 4
        "%> Much longer.\n"  # 5
        "Second line.\n",  # 6
    )
    index = {("s.tex", 1): _at(1, 100.0), ("s.tex", 6): _at(1, 112.0)}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    (choice,) = notes
    assert (choice["place"], choice["id"], choice["start"]) == ("proposal", "p1", 6)
    assert choice["original"] == "Second line."
    assert [o["label"] for o in choice["options"]] == ["shorter", "longer"]
    assert choice["options"][0]["by"] == "agent"
    assert choice["y"] == round(112.0 - 9.0, 2)
