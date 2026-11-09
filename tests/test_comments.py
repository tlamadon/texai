"""Comments written from the page: add, reply, resolve, delete."""

import shlex
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai.annotations import parse_annotations
from texai.comments import (
    CommentError,
    add_comment,
    comment_author,
    delete_annotation,
    format_note,
    reply_to,
    set_status,
)
from texai.config import AppConfig
from texai.server import create_app

PAPER = (
    "\\section{Results}\n"  # 1
    "  We find a large effect.\n"  # 2
    "%@comment by:TL Is it robust?\n"  # 3
    "%@todo -- add the balanced sample\n"  # 4
    "It survives every check.\n"  # 5
)


def test_a_comment_goes_above_the_line_with_an_id_and_its_indent():
    text, comment_id = add_comment(PAPER, 2, "Say how large.", "TL")
    assert comment_id == "c1"
    assert text.split("\n")[1:3] == ["  %@comment id:c1 by:TL Say how large.", "  We find a large effect."]
    (first, *_) = parse_annotations(text)
    assert (first.id, first.by, first.text) == ("c1", "TL", "Say how large.")


def test_a_long_comment_runs_on_and_a_fieldlike_one_is_set_off():
    lines = format_note("comment", "id:x is wrong\nsecond line", by="A B")
    assert lines == ['%@comment by:"A B" -- id:x is wrong', "%> second line"]
    (note,) = parse_annotations("\n".join(lines))
    assert (note.by, note.text) == ("A B", "id:x is wrong\nsecond line")
    with pytest.raises(CommentError):
        add_comment(PAPER, 2, "   ", "TL")


def test_a_reply_names_its_root_giving_it_an_id_and_sits_after_the_thread():
    once = reply_to(PAPER, 3, "comment", "Is it robust?", "Yes, see Table 3.", "agent")
    root, reply, todo = parse_annotations(once)
    assert (root.id, root.by, root.text) == ("c1", "TL", "Is it robust?")
    assert (reply.re, reply.by, reply.line) == ("c1", "agent", 4)
    assert todo.kind == "todo"  # the reply went in before the unrelated todo

    twice = reply_to(once, 3, "comment", "Is it robust?", "Thanks.", "TL")
    found = parse_annotations(twice)
    assert [a.text for a in found[:3]] == ["Is it robust?", "Yes, see Table 3.", "Thanks."]
    assert found[2].re == "c1" and found[2].id not in ("c1", found[1].id)


def test_resolving_and_reopening_keep_everything_else():
    done = set_status(PAPER, 4, "todo", "add the balanced sample", "done")
    assert done.split("\n")[3] == "%@todo status:done add the balanced sample"
    again = set_status(done, 4, "todo", "add the balanced sample", "")
    assert again.split("\n")[3] == "%@todo add the balanced sample"


def test_deleting_takes_the_replies_too():
    threaded = reply_to(PAPER, 3, "comment", "Is it robust?", "Yes.", "agent")
    gone = delete_annotation(threaded, 3, "comment", "Is it robust?")
    assert gone == PAPER.replace("%@comment by:TL Is it robust?\n", "")
    # A reply alone goes alone.
    only = delete_annotation(threaded, 4, "comment", "Yes.")
    assert "Is it robust?" in only and "Yes." not in only


def test_a_comment_that_moved_is_still_found_and_one_that_changed_is_not():
    moved = "New first line.\n" + PAPER
    assert "status:done" in set_status(moved, 4, "todo", "add the balanced sample", "done")
    with pytest.raises(CommentError):
        set_status(PAPER, 4, "todo", "something else entirely", "done")


def test_the_author(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXAI_AUTHOR", "Reviewer 2")
    assert comment_author(tmp_path) == "Reviewer 2"


# ---------------------------------------------------------------- endpoint


@pytest.fixture()
def config(tmp_path: Path, monkeypatch) -> AppConfig:
    monkeypatch.setenv("TEXAI_AUTHOR", "TL")
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text(PAPER)
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    build = f"{shlex.quote(sys.executable)} -c pass"
    return AppConfig.create(root.resolve(), (root / "main.pdf").resolve(), build_command=build)


def test_the_endpoint_writes_and_rebuilds(config):
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert client.get("/api/info").json()["author"] == "TL"

    # A new comment goes into a record, its marker on the sentence: no line
    # moves, so nothing is rebuilt.
    added = client.post("/api/comments", json={"action": "add", "file": "main.tex", "line": 5, "text": "Which checks?"})
    assert added.status_code == 200 and added.json()["build"] is None
    record = added.json()["record"]
    assert (config.root / "main.tex").read_text().endswith(f"It survives every check. %#{record}\n")
    assert '"text": "Which checks?"' in (config.root / "annotations.jsonl").read_text()

    # An inline todo is still resolved in place, and rebuilt.

    resolved = client.post(
        "/api/comments",
        json={"action": "resolve", "file": "main.tex", "line": 4, "kind": "todo", "said": "add the balanced sample"},
    )
    assert resolved.status_code == 200
    assert "%@todo status:done add the balanced sample" in (config.root / "main.tex").read_text()

    stale = client.post(
        "/api/comments",
        json={"action": "delete", "file": "main.tex", "line": 3, "kind": "comment", "said": "not there"},
    )
    assert stale.status_code == 409
