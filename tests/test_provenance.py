"""``%@src`` provenance: how one is read, and what each kind resolves to."""

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai.config import AppConfig
from texai.paragraphs import _map_file
from texai.provenance import (
    ProvenanceError,
    SourceRef,
    parse_source,
    resolve_source,
    serve_path,
)
from texai.server import create_app

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


# ---------------------------------------------------------------- parsing


def test_the_first_word_says_what_kind_of_source():
    assert parse_source("#tab:main") == SourceRef("label", "tab:main")
    assert parse_source("@card2018") == SourceRef("paper", "card2018")
    assert parse_source("code/estimate.py") == SourceRef("file", "code/estimate.py")


def test_a_file_can_name_lines():
    ref = parse_source("code/estimate.py:120-145")
    assert (ref.target, ref.start, ref.end) == ("code/estimate.py", 120, 145)
    one = parse_source("code/estimate.py:7")
    assert (one.start, one.end) == (7, 7)


def test_details_come_in_any_order():
    a = parse_source('@card2018 p.14 "an elasticity of 0.3" sha:1A2B3C4D -- headline')
    b = parse_source('@card2018 sha:1a2b3c4d "an elasticity of 0.3" p.14 -- headline')
    assert a == b
    assert (a.page, a.quote, a.sha, a.note) == (14, "an elasticity of 0.3", "1a2b3c4d", "headline")


def test_curly_quotes_and_stray_words_are_kept():
    ref = parse_source("@k \u201cthe words\u201d see also table 3")
    assert ref.quote == "the words"
    assert ref.note == "see also table 3"


def test_a_quote_may_contain_the_note_separator():
    ref = parse_source('@k "before -- after" -- the note')
    assert (ref.quote, ref.note) == ("before -- after", "the note")


def test_no_target_is_no_source():
    assert parse_source("") is None
    assert parse_source('"just a quote"') is None
    assert parse_source("-- only a note") is None


def test_a_src_line_is_placed_like_a_note_and_carries_its_target(tmp_path):
    path = tmp_path / "s.tex"
    path.write_text("First line.\n%@src @card2018 p.3\nThe claim.\n")
    index = {("s.tex", 1): {1: [100.0, 100.0]}, ("s.tex", 3): {1: [112.0, 112.0]}}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    (note,) = notes
    assert note["kind"] == "src"
    assert note["y"] == round(112.0 - 9.0, 2)  # beside the sentence it backs
    assert note["source"]["type"] == "paper"
    assert note["source"]["page"] == 3


# ---------------------------------------------------------------- resolving


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "build").mkdir(parents=True)
    (root / "papers").mkdir()
    (root / "code").mkdir()
    (root / "build" / "main.pdf").write_bytes(PDF_BYTES)
    (root / "main.tex").write_text(
        "\\begin{document}\n"
        "\\begin{table}\n"
        "\\caption{Main}\\label{tab:main}\n"
        "\\end{table}\n"
        "% \\label{tab:commented}\n"
        "\\end{document}\n"
    )
    (root / "refs.bib").write_text(
        "@article{card2018,\n"
        "  author = {Card, David and Kluve, Jochen and Weber, Andrea},\n"
        "  title = {What Works? {A} Meta Analysis},\n"
        "  year = 2018,\n"
        "  journal = \"JEEA\"\n"
        "}\n"
    )
    (root / "code" / "estimate.py").write_text("a = 1\nb = 2\n")
    return root.resolve()


@pytest.fixture()
def config(project: Path) -> AppConfig:
    return AppConfig.create(project, project / "build" / "main.pdf")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_paper_opens_from_papers_by_its_key_with_its_bib_entry(config, project):
    data = b"%PDF-1.4 card\n"
    (project / "papers" / "card2018.pdf").write_bytes(data)
    answer = resolve_source(config, '@card2018 p.2 "words"')
    assert answer["view"] == "pdf"
    assert answer["path"] == "papers/card2018.pdf"
    assert answer["url"] == "/api/project-file?path=papers/card2018.pdf"
    assert answer["sha"] == _sha(data)
    assert answer["bib"] == {
        "title": "What Works? A Meta Analysis",
        "author": "Card, David and Kluve, Jochen and Weber, Andrea",
        "year": "2018",
        "journal": "JEEA",
    }
    assert "problem" not in answer


def test_a_missing_paper_says_where_to_put_it(config):
    answer = resolve_source(config, "@nobody2020")
    assert answer["view"] == "missing"
    assert "papers/nobody2020.pdf" in answer["problem"]


def test_the_hash_finds_a_renamed_paper(config, project):
    data = b"%PDF-1.4 the real one\n"
    (project / "papers" / "renamed.pdf").write_bytes(data)
    answer = resolve_source(config, f"@card2018 sha:{_sha(data)[:8]}")
    assert answer["view"] == "pdf"
    assert answer["path"] == "papers/renamed.pdf"
    assert answer["moved"] is True


def test_a_changed_paper_still_opens_with_a_warning(config, project):
    (project / "papers" / "card2018.pdf").write_bytes(b"%PDF-1.4 a newer version\n")
    answer = resolve_source(config, "@card2018 sha:00000000")
    assert answer["view"] == "pdf"
    assert answer["stale"] is True
    assert "changed" in answer["problem"]


def test_a_project_file_comes_back_as_text(config):
    answer = resolve_source(config, "code/estimate.py:2")
    assert answer["view"] == "text"
    assert answer["text"] == "a = 1\nb = 2\n"
    assert answer["source"]["start"] == 2


def test_a_project_pdf_is_served_not_inlined(config, project):
    (project / "code" / "fig.pdf").write_bytes(PDF_BYTES)
    answer = resolve_source(config, "code/fig.pdf p.1")
    assert (answer["view"], answer["path"]) == ("pdf", "code/fig.pdf")


def test_files_outside_the_project_or_hidden_are_refused(config, project):
    (project / ".env").write_text("SECRET=1\n")
    assert resolve_source(config, "../outside.txt")["view"] == "missing"
    assert resolve_source(config, ".env")["view"] == "missing"
    with pytest.raises(ProvenanceError):
        serve_path(config, ".git/config")


def test_a_label_is_found_in_the_source_not_in_a_comment(config, monkeypatch):
    import texai.provenance as provenance

    asked = []

    def fake_locate(config, file, line):
        asked.append((file, line))
        return {"found": True, "page": 4, "boxes": [{"page": 4, "x": 1, "y": 2, "width": 3, "height": 4}]}

    monkeypatch.setattr(provenance, "locate", fake_locate)
    answer = resolve_source(config, "#tab:main")
    assert (answer["view"], answer["file"], answer["line"], answer["page"]) == ("pdf", "main.tex", 3, 4)
    assert asked == [("main.tex", 3)]
    assert resolve_source(config, "#tab:commented")["view"] == "missing"


def test_a_label_that_typeset_nothing_borrows_the_line_above(config, monkeypatch):
    import texai.provenance as provenance

    def fake_locate(config, file, line):
        found = line == 2
        return {"found": found, "page": 1 if found else None, "boxes": []}

    monkeypatch.setattr(provenance, "locate", fake_locate)
    assert resolve_source(config, "#tab:main")["page"] == 1


def test_nothing_named_is_an_error(config):
    with pytest.raises(ProvenanceError):
        resolve_source(config, '"only a quote"')


# ---------------------------------------------------------------- endpoints


def test_the_endpoints(config, project):
    (project / "papers" / "card2018.pdf").write_bytes(PDF_BYTES)
    client = TestClient(create_app(config), base_url="http://127.0.0.1")

    answer = client.post("/api/provenance", json={"spec": "@card2018"}).json()
    assert answer["view"] == "pdf"
    served = client.get(answer["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "application/pdf"
    assert served.content == PDF_BYTES

    assert client.post("/api/provenance", json={"spec": "-- nothing"}).status_code == 400
    assert client.post("/api/provenance", json={"source": {"type": "nope"}}).status_code == 400
    assert client.get("/api/project-file", params={"path": "../x"}).status_code == 403
    assert client.get("/api/project-file", params={"path": ".texai/x"}).status_code == 403
    assert client.get("/api/project-file", params={"path": "papers/none.pdf"}).status_code == 404


def test_what_the_margin_has_is_enough_to_peek(config, project, tmp_path):
    """The page peeks with the item /api/paragraphs gave it. A %@src with a
    quote and no note has empty free text, so the parsed source is what must
    travel, not the annotation's text."""
    (project / "materials").mkdir()
    (project / "materials" / "Norwich_JMP.pdf").write_bytes(PDF_BYTES)
    path = tmp_path / "s.tex"
    path.write_text(
        '%@src materials/Norwich_JMP.pdf "Firms in our sample are small" sha:e760a4e2\n'
        "Our firms are small.\n"
    )
    _, (item,) = _map_file(path, "s.tex", {("s.tex", 2): {1: [100.0, 100.0]}}, 72.0, 540.0)
    assert item["text"] == ""  # no note: the old peek sent this, and failed

    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    answer = client.post("/api/provenance", json={"source": item["source"]}).json()
    assert (answer["view"], answer["path"]) == ("pdf", "materials/Norwich_JMP.pdf")
    assert answer["source"]["quote"] == "Firms in our sample are small"
    assert answer["source"]["sha"] == "e760a4e2"


def test_a_source_carries_the_start_of_the_sentence_it_backs(tmp_path):
    path = tmp_path / "s.tex"
    long = "word " * 40
    path.write_text(
        "%@src @a2020\n"  # 1
        "%@src #tab:main\n"  # 2
        "  The   first claim.\n"  # 3
        "%@src @b2021\n"  # 4
        f"{long}\n"  # 5
        "%@src @c2022\n"  # 6
        "\n"  # 7: the paragraph ends before any sentence
    )
    index = {("s.tex", 3): {1: [100.0, 100.0]}, ("s.tex", 5): {1: [112.0, 112.0]}}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0)
    backs = [note["backs"] for note in notes]
    assert backs[:2] == ["The first claim.", "The first claim."]
    assert backs[2].endswith("…") and len(backs[2]) == 120
    assert backs[3] == ""


def test_bib_entries_reads_only_the_keys_asked_for(config):
    from texai.provenance import bib_entries

    assert bib_entries(config, {"card2018", "missing"}) == {
        "card2018": {
            "title": "What Works? A Meta Analysis",
            "author": "Card, David and Kluve, Jochen and Weber, Andrea",
            "year": "2018",
            "journal": "JEEA",
        }
    }
    assert bib_entries(config, set()) == {}
