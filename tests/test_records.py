"""Annotation records in annotations.jsonl, and the markers on their sentences."""

import json
import shlex
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai.config import AppConfig
from texai.paragraphs import _map_file
from texai.record_tools import add_source, annotations_for, comment
from texai.records import (
    RecordError,
    add_marker,
    apply_comment,
    attached_summary,
    externalize,
    load_records,
    markers_in,
    record_at,
    records_path,
    remove_marker,
    save_records,
    sentence_line,
)
from texai.server import create_app

from test_papers import make_pdf


@pytest.fixture()
def config(tmp_path: Path, monkeypatch) -> AppConfig:
    monkeypatch.setenv("TEXAI_AUTHOR", "TL")
    root = tmp_path / "project"
    (root / "paper").mkdir(parents=True)
    (root / "papers").mkdir()
    (root / "papers" / "smith2019.pdf").write_bytes(make_pdf(["The capital share is remarkably stable at one third."]))
    (root / "paper" / "main.tex").write_text(
        "\\begin{document}\n"  # 1
        "We find a large effect.\n"  # 2
        "It is robust \\label{tab:main}.\n"  # 3
        "\\end{document}\n"  # 4
    )
    (root / "paper" / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    build = f"{shlex.quote(sys.executable)} -c pass"
    return AppConfig.create(
        root.resolve(),
        (root / "paper" / "main.pdf").resolve(),
        build_command=build,
        root_tex=(root / "paper" / "main.tex").resolve(),
    )


def test_the_file_sits_beside_the_root_tex_one_sorted_record_a_line(config):
    assert records_path(config) == config.root / "paper" / "annotations.jsonl"
    save_records(config, {"zz": {"id": "zz", "comments": [{"id": "c1"}]}, "aa": {"id": "aa", "sources": [{"t": 1}]}, "empty": {"id": "empty"}})
    lines = records_path(config).read_text().splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["aa", "zz"]  # sorted, the empty one gone
    assert load_records(config)["zz"]["comments"] == [{"id": "c1"}]


def test_markers_sit_at_the_end_of_a_line_and_move_none():
    text = "One.\nTwo.\n"
    marked, record_id = add_marker(text, 2, set())
    assert marked == f"One.\nTwo. %#{record_id}\n" and len(record_id) == 6
    assert markers_in(marked) == [(2, record_id, False)]
    assert record_at(marked, 2) == record_id
    again, same = add_marker(marked, 2, set())
    assert (again, same) == (marked, record_id)  # one record a sentence
    assert remove_marker(marked, record_id) == (text, False)


def test_a_marker_alone_is_about_the_next_line_and_going_takes_its_line():
    text = "%#abcdef\n%@intent why\nThe sentence.\n"
    lines = text.split("\n")
    assert sentence_line(lines, 1, True) == 3
    assert record_at(text, 3) == "abcdef"
    assert remove_marker(text, "abcdef") == ("%@intent why\nThe sentence.\n", True)
    assert markers_in("100\\%#notamark") == []


def test_comments_in_a_record_from_add_to_delete(config):
    added = apply_comment(config, "paper/main.tex", "add", line=2, text="Say how large.", by="TL")
    record = added["record"]
    assert added["texChanged"] and not added["shifted"]
    assert (config.root / "paper" / "main.tex").read_text().split("\n")[1] == f"We find a large effect. %#{record}"

    apply_comment(config, "paper/main.tex", "reply", record_id=record, comment_id="c1", text="Doubled.", by="agent")
    apply_comment(config, "paper/main.tex", "resolve", record_id=record, comment_id="c2")  # a reply resolves its thread
    (thread,) = load_records(config)[record]["comments"]
    assert thread["status"] == "done" and thread["replies"] == [{"id": "c2", "by": "agent", "text": "Doubled."}]

    gone = apply_comment(config, "paper/main.tex", "delete", record_id=record, comment_id="c1")
    assert gone["texChanged"] and record not in load_records(config)
    assert "%#" not in (config.root / "paper" / "main.tex").read_text()
    with pytest.raises(RecordError):
        apply_comment(config, "paper/main.tex", "reply", record_id=record, comment_id="c1", text="?", by="TL")


def test_records_are_drawn_beside_their_sentence(tmp_path):
    path = tmp_path / "s.tex"
    path.write_text("First. %#aaaaaa\nSecond.\n")
    records = {
        "aaaaaa": {
            "id": "aaaaaa",
            "sources": [{"type": "paper", "target": "smith2019", "page": 1, "quote": "one third"}],
            "comments": [{"id": "c1", "kind": "todo", "by": "TL", "text": "Check.", "replies": [{"id": "c2", "by": "agent", "text": "Done."}]}],
        }
    }
    index = {("s.tex", 1): {1: [100.0, 100.0]}, ("s.tex", 2): {1: [112.0, 112.0]}}
    _, notes = _map_file(path, "s.tex", index, 72.0, 540.0, None, records)
    source, todo, reply = notes
    assert (source["place"], source["record"], source["sentence"], source["sentenceText"]) == ("marker", "aaaaaa", 1, "First.")
    assert source["source"]["quote"] == "one third" and source["y"] == round(100.0 - 9.0, 2)
    assert (todo["kind"], todo["id"], todo["cid"], todo["record"]) == ("todo", "aaaaaa:c1", "c1", "aaaaaa")
    assert (reply["re"], reply["text"]) == ("aaaaaa:c1", "Done.")


def test_inline_annotations_move_into_records(config, tmp_path):
    path = config.root / "paper" / "main.tex"
    path.write_text(
        "%@intent stays\n"  # 1
        "%@src @smith2019 p.1 \"one third\"\n"  # 2
        "%@comment id:c1 by:TL Is it?\n"  # 3
        "%@comment id:c2 re:c1 by:agent Yes.\n"  # 4
        "We find a large effect.\n"  # 5
        "%@todo -- check this one\n"  # 6
        "It is robust.\n"  # 7
    )
    records: dict = {}
    new, moved = externalize(config, path, records)
    assert moved == 4
    first, second = sorted(records.values(), key=lambda r: new.index(r["id"]))
    assert new.split("\n")[:3] == ["%@intent stays", f"We find a large effect. %#{first['id']}", f"It is robust. %#{second['id']}"]
    assert first["sources"] == [{"type": "paper", "target": "smith2019", "page": 1, "quote": "one third"}]
    (thread,) = first["comments"]
    assert (thread["by"], thread["text"], thread["replies"][0]["text"]) == ("TL", "Is it?", "Yes.")
    assert second["comments"][0]["kind"] == "todo"


def test_the_agent_tools_check_what_they_record(config):
    assert "Not added: That quote is not in the PDF word for word" in add_source(
        config, "paper/main.tex", 2, "@smith2019", quote="the capital share is remarkably stabel at one third"
    )
    assert "There is no \\label{nope}" in add_source(config, "paper/main.tex", 2, "#nope")
    assert "There is no line 99" in add_source(config, "paper/main.tex", 99, "#tab:main")

    said = add_source(config, "paper/main.tex", 2, "@smith2019", page=4, quote="remarkably stable at one third")
    assert said.startswith("Added source @smith2019 p.1 (on page 1, not 4) to paper/main.tex:2")
    record_id = record_at((config.root / "paper" / "main.tex").read_text(), 2)
    (source,) = load_records(config)[record_id]["sources"]
    assert source["page"] == 1 and len(source["sha"]) == 8

    assert comment(config, "paper/main.tex", 2, "Is one paper enough?") == f"Commented on paper/main.tex:2 as {record_id}:c1."
    assert comment(config, "paper/main.tex", 0, "No.", f"{record_id}:c1") == f"Replied to {record_id}:c1."
    listed = annotations_for(config, "paper/main.tex", 1, 3)
    assert f"paper/main.tex:2 %#{record_id}" in listed and "agent: Is one paper enough?" in listed
    assert "remarkably stable" in attached_summary(config, "paper/main.tex", 2)
    assert annotations_for(config, "paper/main.tex", 3) == "Nothing is attached to paper/main.tex:3."


def test_converting_from_the_page(config):
    path = config.root / "paper" / "main.tex"
    path.write_text("\\begin{document}\n%@comment by:TL Hm.\nWe find it.\n\\end{document}\n")
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    done = client.post("/api/annotations/externalize", json={}).json()
    assert (done["moved"], done["files"], done["build"]["ok"]) == (1, ["paper/main.tex"], True)
    assert "%@comment" not in path.read_text() and "%#" in path.read_text()
    assert client.post("/api/annotations/externalize", json={}).json()["moved"] == 0
