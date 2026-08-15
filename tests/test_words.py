"""Matching a rendered word back to its column in the source."""

from __future__ import annotations

import pytest

from texai.words import locate_word, normalize_rendered, project

SOURCE = r"""\section{Model}

Let output be produced by a constant-returns technology, so that
the \emph{elasticity} of substitution is one. The elasticity
matters because the elasticity governs the response.
A caf\'e in Z\"urich sells na\"ive coffee---really.
The word appears once here: unremarkable.
Total factor productivity $A_{t}$ grows at rate $g$. % elasticity in a comment
Percentages like 40\% and prices like \$5 survive.
""".split("\n")


def column_of(line: str, text: str) -> int:
    return line.index(text) + 1


def find(line: int, phrase: str, before=(), after=(), lines=None):
    return locate_word(lines or SOURCE, line, phrase, list(before), list(after))


# ---------------------------------------------------------------- projection


@pytest.mark.parametrize(
    ("raw", "rendered"),
    [
        (r"the \emph{elasticity} of x", "the elasticity of x"),
        (r"caf\'e and na\"ive", "cafe and naive"),
        (r"a---b and c--d", "a-b and c-d"),
        (r"text % a comment", "text "),
        (r"90\% of \$5", "90% of $5"),
        (r"a~b", "a b"),
        (r"``quoted''", '"quoted"'),
        (r"$x^{2}$ inline", "x^2 inline"),
        (r"\textbf{bold} text", "bold text"),
    ],
)
def test_projection_approximates_the_render(raw: str, rendered: str):
    assert project(raw)[0] == rendered


def test_projection_maps_every_character_back_to_its_column():
    raw = r"the \emph{elasticity} of substitution"
    text, index = project(raw)
    assert len(text) == len(index)
    # Every projected character points at the source character it came from.
    at = text.index("elasticity")
    assert raw[index[at]] == "e"
    assert index[at] == raw.index("elasticity")


def test_projection_of_an_escaped_percent_is_not_a_comment():
    text, _ = project(r"a 50\% share % but this is a comment")
    assert text == "a 50% share "


# ---------------------------------------------------------------- normalising


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("ﬁnance", "finance"),
        ("eﬀort", "effort"),
        ("“quoted”", '"quoted"'),
        ("it’s", "it's"),
        ("em—dash", "em-dash"),
        ("en–dash", "en-dash"),
        ("spaced out", "spaced out"),
        ("  padded  ", "padded"),
    ],
)
def test_rendered_text_is_folded_toward_the_source(rendered: str, expected: str):
    assert normalize_rendered(rendered) == expected


# ---------------------------------------------------------------- matching


def test_finds_a_word_wrapped_in_markup():
    hit = find(4, "elasticity", before=["the"], after=["of", "substitution"])
    assert hit is not None
    assert (hit.line, hit.column) == (4, column_of(SOURCE[3], "elasticity"))
    assert hit.exact is True


def test_looks_past_the_line_synctex_reported():
    """A wrapped paragraph puts the word a line or two after the box start."""
    hit = find(3, "substitution")
    assert hit is not None
    assert hit.line == 4
    assert hit.column == column_of(SOURCE[3], "substitution")


def test_context_picks_the_right_repeat_on_one_line():
    line = SOURCE[3]
    first = find(4, "elasticity", before=["the"], after=["of"])
    second = find(4, "elasticity", before=["one.", "The"], after=["matters"])

    assert first.column == line.index("elasticity") + 1
    assert second.column == line.rindex("elasticity") + 1
    assert first.column != second.column


def test_context_picks_the_right_repeat_across_lines():
    hit = find(4, "elasticity", before=["because", "the"], after=["governs"])
    assert hit is not None
    assert hit.line == 5
    assert hit.column == column_of(SOURCE[4], "elasticity")


def test_an_accent_macro_still_matches_its_rendered_letter():
    for rendered, source_text in [("café", r"caf\'e"), ("Zürich", r"Z\"urich"), ("naïve", r"na\"ive")]:
        hit = find(6, rendered)
        assert hit is not None, rendered
        assert hit.line == 6
        assert hit.column == column_of(SOURCE[5], source_text)
        assert hit.exact is False  # reached by folding, and says so


def test_a_word_before_an_em_dash():
    hit = find(6, "coffee")
    assert hit is not None
    assert hit.column == column_of(SOURCE[5], "coffee")


def test_a_comment_is_never_matched():
    """The only "elasticity" within reach of line 8 is inside a comment."""
    hit = find(8, "elasticity")
    # It finds the real one on line 5, never the commented one on line 8.
    assert hit is None or hit.line != 8


def test_an_escaped_percent_is_matched_not_skipped():
    hit = find(9, "40%")
    assert hit is not None
    assert hit.column == column_of(SOURCE[8], "40")


def test_a_phrase_matches_as_one_run():
    hit = find(4, "of substitution")
    assert hit is not None
    assert hit.column == column_of(SOURCE[3], "of substitution")


def test_a_phrase_that_wraps_falls_back_to_its_first_word():
    hit = find(4, "elasticity of substitution", after=["is", "one"])
    assert hit is not None
    assert hit.column == column_of(SOURCE[3], "elasticity")


# ---------------------------------------------------------------- refusing


def test_refuses_a_word_that_is_not_there():
    assert find(4, "chartreuse") is None


def test_refuses_a_substring_of_a_longer_word():
    assert find(7, "remark") is None  # inside "unremarkable"
    assert find(4, "last") is None  # inside "elasticity"


@pytest.mark.parametrize("phrase", ["", " ", "a", None, "x" * 300])
def test_refuses_unusable_input(phrase):
    assert find(4, phrase) is None


def test_the_reported_line_wins_over_its_neighbours():
    lines = ["same word here", "same word here", "same word here"]
    hit = locate_word(lines, 2, "word")
    assert hit is not None
    assert hit.line == 2


def test_refuses_when_two_lines_are_equally_plausible():
    """Being wrong about the line is worse than having no column at all.

    The word is absent from the line SyncTeX gave and equidistant either side,
    with nothing in the context to break the tie.
    """
    lines = ["the word here", "nothing of interest", "the word here"]
    assert locate_word(lines, 2, "word") is None


def test_context_breaks_a_tie_that_distance_cannot():
    lines = ["alpha word one", "nothing of interest", "beta word two"]
    hit = locate_word(lines, 2, "word", ["beta"], ["two"])
    assert hit is not None
    assert hit.line == 3


def test_a_tie_inside_one_line_takes_the_first():
    """The line is right either way, so the leftmost is a safe answer."""
    lines = ["alpha and alpha again"]
    hit = locate_word(lines, 1, "alpha")
    assert hit is not None
    assert hit.column == 1


def test_stays_within_its_window():
    lines = ["needle"] + ["filler"] * 20 + ["needle"]
    assert locate_word(lines, 12, "needle") is None  # both are out of reach


def test_handles_an_empty_file():
    assert locate_word([], 1, "anything") is None


def test_handles_a_line_number_past_the_end():
    """A line beyond the file is nonsense, but a unique word is still unambiguous."""
    hit = locate_word(["one line"], 500, "line")
    assert hit is not None
    assert hit.line == 1  # never outside the file, whatever it was told


def test_never_returns_a_column_outside_its_line():
    for line_number in range(1, len(SOURCE) + 1):
        for word in ("elasticity", "coffee", "productivity", "the"):
            hit = find(line_number, word)
            if hit is None:
                continue
            assert 1 <= hit.column <= len(SOURCE[hit.line - 1]) + 1
            assert 1 <= hit.line <= len(SOURCE)


# ---------------------------------------------------------------- far away


FAR = r"""\title{A Study of Monopsony Power}
\author{Someone}
\begin{document}
\maketitle
\section{Introduction}
Ordinary prose that mentions power and power again.
\begin{figure}
\caption{Employment responses by decile}
\end{figure}
Later text.
""".split("\n")


def test_text_that_renders_far_from_where_it_is_written():
    """\\maketitle typesets the title pages away from the \\title line.

    SyncTeX points at the command, not the words, so proximity is useless here.
    """
    hit = locate_word(FAR, 4, "Monopsony")  # \maketitle is line 4, the word is on line 1
    assert hit is not None
    assert hit.line == 1
    assert hit.column == column_of(FAR[0], "Monopsony")


def test_a_float_caption_far_from_its_placement():
    hit = locate_word(FAR, 10, "decile")
    assert hit is not None
    assert hit.line == 8


def test_the_distant_pass_demands_a_unique_answer():
    """Two distant candidates and no proximity to choose between them: say nothing."""
    lines = ["\\maketitle"] + ["filler"] * 8 + ["power here", "and power there"]
    assert locate_word(lines, 1, "power") is None


def test_a_word_on_the_reported_line_is_still_preferred():
    """Even when the same word appears elsewhere, the line clicked wins."""
    hit = locate_word(FAR, 1, "Monopsony")
    assert hit is not None
    assert hit.line == 1


def test_a_near_match_still_wins_over_a_distant_unique_one():
    lines = ["the needle here", "filler", "filler", "filler", "filler", "a needle far away"]
    hit = locate_word(lines, 1, "needle")
    assert hit is not None
    assert hit.line == 1


def test_the_distant_pass_still_respects_word_boundaries():
    lines = ["\\maketitle"] + ["filler"] * 8 + ["unremarkable prose"]
    assert locate_word(lines, 1, "remark") is None


def test_the_distant_pass_still_ignores_comments():
    lines = ["\\maketitle"] + ["filler"] * 8 + ["% monopsony lives only in a comment"]
    assert locate_word(lines, 1, "monopsony") is None
