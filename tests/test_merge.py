"""Folding an outside edit into an unsaved buffer."""

from __future__ import annotations

import pytest

from texai.merge import merge3

BASE = """\\section{Model}
Workers choose hours.
The wage is exogenous.

\\section{Data}
We use the CPS.
Sample: 1990--2020.
"""


def apply_edits(text: str, edits) -> str:
    """What the editor does with the splices it is handed."""
    out = text
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        out = out[: edit.start] + edit.text + out[edit.end :]
    return out


def test_untouched_disk_is_a_no_op():
    ours = BASE.replace("exogenous", "endogenous")
    merged = merge3(BASE, ours, BASE)

    assert merged.clean
    assert merged.text == ours
    assert merged.edits == ()


def test_clean_buffer_takes_the_new_version_whole():
    theirs = BASE.replace("the CPS", "the PSID")
    merged = merge3(BASE, BASE, theirs)

    assert merged.clean
    assert merged.text == theirs
    assert apply_edits(BASE, merged.edits) == theirs


def test_edits_in_different_places_both_survive():
    ours = BASE.replace("exogenous", "endogenous")
    theirs = BASE.replace("the CPS", "the PSID")

    merged = merge3(BASE, ours, theirs)

    assert merged.clean
    assert "endogenous" in merged.text  # the unsaved edit
    assert "the PSID" in merged.text  # the agent's
    assert apply_edits(ours, merged.edits) == merged.text


def test_an_insertion_above_does_not_disturb_the_line_below():
    ours = BASE.replace("Sample: 1990--2020.", "Sample: 1990--2019.")
    theirs = BASE.replace("\\section{Model}\n", "\\section{Model}\n\\label{sec:model}\n")

    merged = merge3(BASE, ours, theirs)

    assert merged.clean
    assert merged.text == (
        BASE.replace("\\section{Model}\n", "\\section{Model}\n\\label{sec:model}\n").replace(
            "Sample: 1990--2020.", "Sample: 1990--2019."
        )
    )


def test_edits_to_the_same_line_are_a_conflict():
    ours = BASE.replace("The wage is exogenous.", "The wage is endogenous.")
    theirs = BASE.replace("The wage is exogenous.", "Wages are taken as given.")

    merged = merge3(BASE, ours, theirs)

    assert not merged.clean
    assert merged.text == ours  # the buffer is left exactly as it was
    assert merged.edits == ()


def test_insertions_at_the_same_point_are_a_conflict():
    ours = BASE.replace("\\section{Data}", "\\subsection{Timing}\n\n\\section{Data}")
    theirs = BASE.replace("\\section{Data}", "\\subsection{Wages}\n\n\\section{Data}")

    assert not merge3(BASE, ours, theirs).clean


def test_the_same_edit_on_both_sides_lands_once():
    same = BASE.replace("the CPS", "the PSID")

    merged = merge3(BASE, same, same)

    assert merged.clean
    assert merged.text == same
    assert merged.text.count("the PSID") == 1
    assert merged.edits == ()


def test_a_rewrite_of_the_whole_file_conflicts_with_any_local_edit():
    ours = BASE.replace("Workers", "Households")
    theirs = "\n".join(f"line {i}" for i in range(10)) + "\n"

    assert not merge3(BASE, ours, theirs).clean


def test_a_rewrite_of_the_whole_file_lands_on_a_clean_buffer():
    theirs = "\n".join(f"line {i}" for i in range(10)) + "\n"

    merged = merge3(BASE, BASE, theirs)

    assert merged.clean
    assert merged.text == theirs


def test_deletion_on_one_side_and_an_edit_far_away_on_the_other():
    ours = BASE.replace("Sample: 1990--2020.\n", "")
    theirs = BASE.replace("Workers choose hours.", "Workers choose hours and effort.")

    merged = merge3(BASE, ours, theirs)

    assert merged.clean
    assert "Sample" not in merged.text
    assert "hours and effort" in merged.text


def test_edits_carry_the_offsets_the_editor_needs():
    ours = BASE
    theirs = BASE.replace("We use the CPS.", "We use the PSID.")

    merged = merge3(BASE, ours, theirs)

    (edit,) = merged.edits
    assert ours[edit.start : edit.end] == "We use the CPS.\n"
    assert edit.text == "We use the PSID.\n"


def test_appending_to_the_end_of_a_file_without_a_final_newline():
    base = "one\ntwo"
    merged = merge3(base, base, "one\ntwo\nthree")

    assert merged.clean
    assert merged.text == "one\ntwo\nthree"
    assert apply_edits(base, merged.edits) == "one\ntwo\nthree"


def test_line_endings_are_normalised_before_comparing():
    """A file written back with CRLF is not three hundred changed lines."""
    crlf = BASE.replace("\n", "\r\n")

    merged = merge3(BASE, BASE, crlf)

    assert merged.clean
    assert merged.edits == ()
    assert merged.text == BASE


def test_an_empty_file_on_either_side():
    assert merge3("", "", "hello\n").text == "hello\n"
    assert merge3("hello\n", "hello\n", "").text == ""
    assert merge3("", "mine\n", "theirs\n").clean is False


@pytest.mark.parametrize("gap", [0, 1, 2, 5])
def test_neighbouring_edits_merge_once_they_are_a_line_apart(gap: int):
    """Touching changes are a conflict; separated ones are not."""
    lines = [f"line {i}\n" for i in range(20)]
    base = "".join(lines)

    ours = list(lines)
    ours[5] = "mine\n"
    theirs = list(lines)
    theirs[5 + gap] = "theirs\n"

    merged = merge3(base, "".join(ours), "".join(theirs))

    assert merged.clean is (gap > 0)
    if merged.clean:
        assert "mine\n" in merged.text
        assert "theirs\n" in merged.text
