"""Which page is which slide, from the SyncTeX data of a beamer deck."""

import gzip
from pathlib import Path

import pytest

from texai.config import AppConfig
from texai.slides import frame_for_page, slide_label, slide_map

TALK = r"""\documentclass{beamer}
\begin{document}
\frame{\titlepage}
\begin{frame}{First}
\begin{itemize}
  \item<1-> One
  \item<2-> Two
\end{itemize}
\end{frame}
\begin{frame}[fragile]{Second}
Some words.
\end{frame}
\end{document}
"""


def synctex(root: Path, pages: dict[int, list[tuple[int, int, int]]]) -> bytes:
    """A SyncTeX file with, per page, ``(tag, line, glyphs)`` records."""
    out = [
        "SyncTeX Version:1",
        f"Input:1:{root}/talk.tex",
        f"Input:2:{root}/talk.vrb",
        "Magnification:1000",
        "Unit:1",
        "X Offset:0",
        "Y Offset:0",
        "Content:",
    ]
    for page, records in pages.items():
        out.append(f"{{{page}")
        for tag, line, glyphs in records:
            out.extend(f"x{tag},{line}:{1000000 + i},{2000000}" for i in range(glyphs))
        out.append(f"}}{page}")
    return gzip.compress("\n".join(out).encode() + b"\n")


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = (tmp_path / "deck").resolve()
    root.mkdir()
    (root / "talk.tex").write_text(TALK)
    (root / "talk.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (root / "talk.synctex.gz").write_bytes(
        synctex(
            root,
            {
                1: [(1, 3, 30), (1, 9, 3)],  # the title page, with a stray record
                2: [(1, 9, 40)],  # the first frame, twice over: overlays
                3: [(1, 9, 50)],
                4: [(1, 12, 20), (2, 2, 60)],  # fragile: its body is in the .vrb
            },
        )
    )
    return AppConfig.create(root, root / "talk.pdf")


def test_each_frame_gets_the_pages_its_closing_line_fills(config: AppConfig):
    frames = slide_map(config)["frames"]
    assert [(f["file"], f["start"], f["end"], f["title"], f["pages"]) for f in frames] == [
        ("talk.tex", 3, 3, "", [1]),  # the stray record does not claim page 1
        ("talk.tex", 4, 9, "First", [2, 3]),
        ("talk.tex", 10, 12, "Second", [4]),
    ]
    assert [b["kind"] for b in frames[1]["blocks"]] == ["title", "item", "item"]


def test_a_page_names_its_frame(config: AppConfig):
    assert frame_for_page(config, 3)["title"] == "First"
    assert frame_for_page(config, 4)["title"] == "Second"
    assert frame_for_page(config, 9) is None


def test_the_map_follows_the_source_without_a_rebuild(config: AppConfig):
    assert slide_map(config) is slide_map(config)  # cached
    source = config.root / "talk.tex"
    source.write_text(TALK.replace("{First}", "{Renamed}"))
    assert slide_map(config)["frames"][1]["title"] == "Renamed"


def test_a_paper_has_no_slides(tmp_path: Path):
    root = (tmp_path / "paper").resolve()
    root.mkdir()
    (root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n")
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    paper = AppConfig.create(root, root / "main.pdf")
    assert slide_map(paper) == {"frames": []}


def test_a_line_on_a_slide_is_labelled_with_it(config: AppConfig):
    assert slide_label(config, "talk.tex", 6) == '"First" (talk.tex:4-9)'
    assert slide_label(config, "talk.tex", 3) == "untitled (talk.tex:3-3)"
    assert slide_label(config, "talk.tex", 2) == ""


def test_a_line_inside_a_frame_is_found_on_its_slide(config: AppConfig, monkeypatch):
    # Asked about a line inside a frame, SyncTeX answers with every box of the
    # slide on every overlay page. The source knows better: one slide, one box.
    from texai.navigate import locate
    from texai.synctex import SyncTexBox

    everything = [SyncTexBox(page=p, x=0.0, y=0.0, width=400.0, height=200.0) for p in (2, 2, 3, 3)]
    monkeypatch.setattr("texai.navigate.run_synctex_view", lambda *a, **k: everything)
    found = locate(config, "talk.tex", 6)
    assert found["found"] is True
    # The last page of the frame: every overlay uncovered.
    assert found["page"] == 3 and [b["page"] for b in found["boxes"]] == [3]
    assert found["slide"] == {"start": 4, "end": 9, "title": "First"}

    # Outside any frame SyncTeX's answer stands.
    assert len(locate(config, "talk.tex", 2)["boxes"]) == 4
