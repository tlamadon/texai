"""Frames and their blocks, read from the source of a beamer deck."""

from texai.frames import frame_at, frames_in

DECK = r"""\documentclass{beamer}
\begin{document}
\frame{\titlepage}
\begin{frame}[t]{Identification \& data}
We compare movers and stayers.

\begin{itemize}[<+->]
  \item<1-> Movers switch firms
  \item<2-> Stayers do not,
    and that matters
    \begin{itemize}
      \item which is the control
    \end{itemize}
  \item $y = x\beta$
\end{itemize}

\begin{columns}
\column{0.5\textwidth}
Left column words.
\column{0.5\textwidth}
Right column words.
\end{columns}
\end{frame}
% \begin{frame} a frame that is commented out
\begin{frame}
\frametitle{Second}
Only prose here.
\end{frame}
\end{document}
""".split("\n")


def blocks(frame):
    return [(b.kind, b.start, b.end, b.text) for b in frame.blocks]


def test_both_forms_of_frame_are_found_and_comments_are_not():
    frames = frames_in(DECK)
    assert [(f.start, f.end, f.title) for f in frames] == [
        (3, 3, ""),
        (4, 23, "Identification & data"),
        (25, 28, "Second"),
    ]


def test_a_frame_is_its_title_its_items_and_its_paragraphs():
    frame = frames_in(DECK)[1]
    assert blocks(frame) == [
        ("title", 4, 4, "Identification & data"),
        ("text", 5, 5, "We compare movers and stayers."),
        ("item", 8, 8, "Movers switch firms"),
        # An item runs to the next one, sub-list and all; its text is its own.
        ("item", 9, 13, "Stayers do not, and that matters"),
        ("item", 12, 12, "which is the control"),
        ("item", 14, 14, "y = x"),
        # A \column between two columns' words ends a paragraph as a blank line does.
        ("text", 19, 19, "Left column words."),
        ("text", 21, 21, "Right column words."),
    ]
    assert [b.level for b in frame.blocks if b.kind == "item"] == [0, 0, 1, 0]


def test_a_frametitle_inside_the_frame_is_its_title():
    frame = frames_in(DECK)[2]
    assert blocks(frame) == [("title", 26, 26, "Second"), ("text", 27, 27, "Only prose here.")]


def test_the_closing_line_belongs_to_the_frame():
    # \end{frame} is the line SyncTeX gives for everything on the slide.
    assert frame_at(DECK, 23).start == 4
    assert frame_at(DECK, 10).start == 4
    assert frame_at(DECK, 24) is None
    assert frame_at(DECK, 2) is None


def test_a_frame_left_open_is_not_a_frame():
    assert frames_in(["\\begin{frame}{Half typed}", "\\item one"]) == []


def test_a_multi_line_frame_command_has_blocks_too():
    lines = ["\\frame{", "  \\frametitle{Plan}", "  \\begin{itemize}", "  \\item First", "  \\end{itemize}", "}"]
    (frame,) = frames_in(lines)
    assert (frame.start, frame.end, frame.title) == (1, 6, "Plan")
    assert blocks(frame) == [("title", 2, 2, "Plan"), ("item", 4, 4, "First")]


def test_items_of_a_list_no_environment_opened_still_count():
    lines = ["\\begin{frame}", "\\mylist", "\\item One thing", "\\item Another thing", "\\end{frame}"]
    (frame,) = frames_in(lines)
    assert blocks(frame) == [("item", 3, 3, "One thing"), ("item", 4, 4, "Another thing")]


def test_a_paragraph_that_opens_an_environment_is_the_whole_of_it():
    lines = [
        "\\begin{frame}{Take-away}",
        "\\begin{block}{Main point}",
        "Effects understate the cost.",
        "\\end{block}",
        "",
        "\\begin{center}",
        "Centred words.",
        "\\end{center}",
        "",
        "\\begin{alertblock}{Careful}",
        "",
        "First paragraph of it.",
        "",
        "Last paragraph of it. \\end{alertblock}",
        "\\end{frame}",
    ]
    (frame,) = frames_in(lines)
    assert blocks(frame) == [
        ("title", 1, 1, "Take-away"),
        # With its \end{block}: editing it cannot leave the environment open.
        ("text", 2, 4, "Main point Effects understate the cost."),
        # Nothing to keep whole here: the words alone are the paragraph.
        ("text", 7, 7, "Centred words."),
        ("text", 10, 14, "Careful First paragraph of it. Last paragraph of it."),
        ("text", 12, 12, "First paragraph of it."),
    ]
