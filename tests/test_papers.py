"""``read_paper``: a PDF as text for the agent, and where a quote is in it."""

import hashlib
from pathlib import Path

import pytest

from texai.config import AppConfig
from texai.papers import fold, read_paper


def make_pdf(pages: list[str]) -> bytes:
    """A small valid PDF, one line of Helvetica text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 10 Tf 40 700 Td ({escaped}) Tj ET".encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {4 + 2 * i} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


PAGES = [
    "Introduction. We study capital shares.",
    "Across countries the capital share is remarkably stable at one third.",
    "Robustness. The share is stable in every decade.",
]


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "project"
    (root / "papers").mkdir(parents=True)
    (root / "papers" / "smith2019.pdf").write_bytes(make_pdf(PAGES))
    (root / "figs").mkdir()
    (root / "figs" / "table.pdf").write_bytes(make_pdf(["Table 2. Estimates."]))
    (root / "notes.txt").write_text("not a pdf\n")
    (root / "refs.bib").write_text("@article{smith2019, author = {Smith, Jane}, title = {Shares}, year = 2019}\n")
    (root / "main.pdf").write_bytes(make_pdf(["main"]))
    return AppConfig.create(root.resolve(), (root / "main.pdf").resolve())


def sha8(config: AppConfig) -> str:
    return hashlib.sha256((config.root / "papers" / "smith2019.pdf").read_bytes()).hexdigest()[:8]


def test_folding_keeps_only_what_survives_extraction():
    assert fold("Élas-\nticity, ﬁnal") == "elasticityfinal"


def test_a_quote_is_found_with_its_page_and_a_ready_source_line(config):
    answer = read_paper(config, "@smith2019", find="the capital share is remarkably stable")
    assert answer.splitlines()[0] == f"papers/smith2019.pdf — 3 pages, sha:{sha8(config)}"
    assert "Smith, Jane, 2019. Shares" in answer
    assert "Passage found on page 2:" in answer
    assert "«the capital share is remarkably stable»" in answer
    assert f'%@src @smith2019 p.2 "the capital share is remarkably stable" sha:{sha8(config)}' in answer


def test_a_misquote_finds_the_closest_passage_and_says_so(config):
    answer = read_paper(config, "smith2019", find="the capital share is remarkably stabel at one third")
    assert "closest match on page 2" in answer
    assert "%@src" not in answer  # only exact quotes get a line to paste


def test_a_passage_on_several_pages_says_where_else(config):
    assert "too short" in read_paper(config, "@smith2019", find="share")
    answer = read_paper(config, "@smith2019", find="capital share")
    assert "Passage found on page 1:" in answer
    assert "It also occurs on page(s) 2; quote more to pin one down." in answer


def test_missing_text(config):
    assert "not in this PDF's text" in read_paper(config, "@smith2019", find="nothing like this here")


def test_pages_are_read_in_the_files_own_count(config):
    answer = read_paper(config, "@smith2019", pages="2-3")
    assert "--- page 2 of 3 ---\nAcross countries" in answer
    assert "--- page 3 of 3 ---\nRobustness" in answer
    assert "Introduction" not in answer
    everything = read_paper(config, "@smith2019")
    assert all(f"--- page {n} of 3 ---" in everything for n in (1, 2, 3))
    assert 'Pages are written "3"' in read_paper(config, "@smith2019", pages="two")
    assert "No such pages" in read_paper(config, "@smith2019", pages="9")


def test_a_long_document_comes_in_parts(config, monkeypatch):
    import texai.papers as papers

    monkeypatch.setattr(papers, "MAX_CHARS", 80)
    answer = read_paper(config, "@smith2019")
    assert "--- page 1 of 3 ---" in answer and "--- page 3 of 3 ---" not in answer
    assert 'ask for pages="' in answer


def test_targets(config):
    assert "figs/table.pdf — 1 page," in read_paper(config, "figs/table.pdf")
    assert "Papers available: @smith2019." in read_paper(config, "@nobody")
    assert "not a PDF" in read_paper(config, "notes.txt")
    assert "outside the project" in read_paper(config, "../elsewhere.pdf")
    assert "Name a paper" in read_paper(config, "#tab:main")


def test_a_pdf_with_no_text_says_so(config):
    (config.root / "papers" / "scan.pdf").write_bytes(make_pdf([""]))
    assert "no text layer" in read_paper(config, "@scan", find="anything at all")
