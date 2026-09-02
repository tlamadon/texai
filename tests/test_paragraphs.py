"""The pure half of the paragraph guide: enumeration and the preamble split.

These two functions are the contract the two panes rely on — the editor and the
backend must agree on which lines form which paragraph, and on the index each
one gets, or a block would wear one colour in the source and another on the
page. The forward mapping through SyncTeX is exercised elsewhere; here we pin
the numbering.
"""

from texai.paragraphs import (
    PARAGRAPH_PALETTE,
    _body_start_line,
    paragraph_ranges,
)


def test_paragraph_ranges_splits_on_blank_lines():
    text = "one\ntwo\n\n\nthree\n\nfour\n"
    assert paragraph_ranges(text) == [(0, 1, 2), (1, 5, 5), (2, 7, 7)]


def test_paragraph_ranges_matches_editor_blank_rule():
    # A line of only whitespace is a break, exactly as `line.trim() === ''` is
    # in editor.js; the paragraph index is its ordinal from the top of the file.
    text = "alpha\n   \nbeta\n\t\ngamma"
    assert paragraph_ranges(text) == [(0, 1, 1), (1, 3, 3), (2, 5, 5)]


def test_paragraph_ranges_no_trailing_newline():
    assert paragraph_ranges("solo") == [(0, 1, 1)]


def test_paragraph_ranges_empty_is_empty():
    assert paragraph_ranges("") == []
    assert paragraph_ranges("\n\n") == []


def test_indexing_survives_preamble_so_palette_stays_in_step():
    # The preamble paragraphs keep their indices even though they draw no bar,
    # so the first body paragraph lands on the same palette colour the editor
    # gives it. Here `\begin{document}` is line 5, and the two preamble
    # paragraphs are indices 0 and 1, leaving the body to start at index 2.
    text = (
        "\\documentclass{article}\n"  # 1  index 0
        "\n"  # 2
        "\\title{T}\n"  # 3  index 1
        "\n"  # 4
        "\\begin{document}\n"  # 5  index 2  (body starts here)
        "\n"  # 6
        "Body paragraph.\n"  # 7  index 3
    )
    ranges = paragraph_ranges(text)
    assert ranges[0] == (0, 1, 1)
    assert ranges[1] == (1, 3, 3)
    assert _body_start_line(text) == 5
    body = [(i, a, b) for (i, a, b) in ranges if b >= _body_start_line(text)]
    assert body[0][0] == 2  # the \begin{document}/... paragraph keeps index 2
    assert body[-1] == (3, 7, 7)
    # Its colour is index % palette, the same expression editor.js uses.
    assert body[-1][0] % PARAGRAPH_PALETTE == 3


def test_body_start_defaults_to_one_without_preamble():
    # An \input file has no \begin{document}; every line is body.
    assert _body_start_line("just prose\n\nmore prose\n") == 1
