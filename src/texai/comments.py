"""Comments written from the page: add one, reply, resolve, delete.

Comments are ``%@`` annotations like any other (see :mod:`texai.annotations`),
so these are small, careful rewrites of the source: a new ``%@comment`` above
the sentence it is about, a reply below the thread it answers, a ``status:``
field set or cleared, a thread taken out. They are the reader's own edits, not
the agent's, so the server saves them the way a hand edit in the editor is
saved, and rebuilds.

An annotation is named by the line it was on when the page was built, and by
what it says. The file may have moved since — another comment added above it,
an edit in the editor — so the line is only where to look first; what it says
is what must match.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .annotations import Annotation, kind_of, parse_annotations, parse_body

__all__ = [
    "CommentError",
    "add_comment",
    "comment_author",
    "delete_annotation",
    "find_annotation",
    "format_note",
    "reply_to",
    "set_status",
]


class CommentError(ValueError):
    """The comment is not where, or not what, the page said it was."""


_ID = re.compile(r"\bid:c(\d+)\b")


def _free_id(text: str) -> str:
    taken = {int(match.group(1)) for match in _ID.finditer(text)}
    number = 1
    while number in taken:
        number += 1
    return f"c{number}"


def _value(value: str) -> str:
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def format_note(
    kind: str,
    text: str,
    *,
    id: str = "",
    by: str = "",
    re: str = "",
    status: str = "",
    indent: str = "",
) -> list[str]:
    """A free-text annotation as source lines: the ``%@`` line, then a ``%>``
    line for each further line of text."""
    first, *rest = text.strip().split("\n") if text.strip() else [""]
    head = [f"%@{kind}"]
    for key, value in (("id", id), ("by", by), ("re", re), ("status", status)):
        if value:
            head.append(f"{key}:{_value(value)}")
    fields = " ".join(head)
    line = f"{fields} {first}".rstrip()
    # A text whose first word would read as a field is set off with `--`.
    if first and parse_body(kind, line.split(" ", 1)[1] if " " in line else "")["text"] != first.strip():
        line = f"{fields} -- {first}"
    return [f"{indent}{line}"] + [f"{indent}%> {more}".rstrip() for more in rest]


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def find_annotation(text: str, line: int, kind: str, said: str) -> Annotation:
    """The annotation the page showed at ``line``, saying ``said``.

    At that line if it is still there; otherwise the one annotation of that
    kind in the file that says the same, wherever it moved to.
    """
    found = parse_annotations(text)
    same = [a for a in found if a.kind == kind and a.text == said]
    for annotation in same:
        if annotation.line == line:
            return annotation
    if len(same) == 1:
        return same[0]
    raise CommentError(
        "That comment has changed in the source since the page was built; "
        "wait for the rebuild, or edit it in the source."
    )


def _replace(lines: list[str], first: int, last: int, new: list[str]) -> list[str]:
    return lines[: first - 1] + new + lines[last:]


def add_comment(text: str, line: int, body: str, by: str) -> tuple[str, str]:
    """A new ``%@comment`` above line ``line``. Returns the file and its id."""
    if not body.strip():
        raise CommentError("Write something first.")
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        raise CommentError(f"There is no line {line} in that file.")
    comment_id = _free_id(text)
    new = format_note("comment", body, id=comment_id, by=by, indent=_indent(lines[line - 1]))
    return "\n".join(lines[: line - 1] + new + lines[line - 1 :]), comment_id


def _thread(found: list[Annotation], root: Annotation) -> list[Annotation]:
    """The root and every reply under it, wherever they are in the file."""
    ids = {root.id} if root.id else set()
    thread = [root]
    changed = True
    while changed:
        changed = False
        for annotation in found:
            if annotation not in thread and annotation.re and annotation.re in ids:
                thread.append(annotation)
                if annotation.id:
                    ids.add(annotation.id)
                changed = True
    return thread


def reply_to(text: str, line: int, kind: str, said: str, body: str, by: str) -> str:
    """A reply to the annotation at ``line``, after the last of its thread
    that sits with it. A root without an id gets one, so the reply can name it."""
    if not body.strip():
        raise CommentError("Write something first.")
    root = find_annotation(text, line, kind, said)
    if kind_of(root.kind).structured:
        raise CommentError(f"A %@{root.kind} cannot be replied to.")
    lines = text.split("\n")
    found = parse_annotations(text)
    indent = _indent(lines[root.line - 1])

    if not root.id:
        fields = {"id": _free_id(text), "by": root.by, "re": root.re, "status": root.status}
        rewritten = format_note(root.kind, root.text, indent=indent, **fields)
        lines = _replace(lines, root.line, root.end, rewritten)
        text = "\n".join(lines)
        found = parse_annotations(text)
        root = next(a for a in found if a.line == root.line)

    # After the last annotation of the thread in the run of annotations that
    # starts at the root, so the conversation reads top to bottom.
    thread = _thread(found, root)
    after = root.end
    for annotation in found:
        if annotation.line == after + 1:
            if annotation in thread:
                after = annotation.end
            else:
                break
    reply = format_note("comment", body, id=_free_id(text), by=by, re=root.id, indent=indent)
    return "\n".join(lines[:after] + reply + lines[after:])


def set_status(text: str, line: int, kind: str, said: str, status: str) -> str:
    """The annotation at ``line`` with its ``status:`` set, or cleared."""
    annotation = find_annotation(text, line, kind, said)
    if kind_of(annotation.kind).structured:
        raise CommentError(f"A %@{annotation.kind} has no status to set here.")
    lines = text.split("\n")
    rewritten = format_note(
        annotation.kind,
        annotation.text,
        id=annotation.id,
        by=annotation.by,
        re=annotation.re,
        status=status,
        indent=_indent(lines[annotation.line - 1]),
    )
    return "\n".join(_replace(lines, annotation.line, annotation.end, rewritten))


def delete_annotation(text: str, line: int, kind: str, said: str) -> str:
    """The file without the annotation at ``line``, or the replies to it."""
    annotation = find_annotation(text, line, kind, said)
    found = parse_annotations(text)
    doomed = _thread(found, annotation)
    lines = text.split("\n")
    for gone in sorted(doomed, key=lambda a: a.line, reverse=True):
        lines = _replace(lines, gone.line, gone.end, [])
    return "\n".join(lines)


# ------------------------------------------------------------------ who

_author_cache: dict[Path, str] = {}


def comment_author(root: Path) -> str:
    """Who is writing: ``TEXAI_AUTHOR``, else git's ``user.name`` as initials
    (one word stays as it is), else ``me``."""
    explicit = os.environ.get("TEXAI_AUTHOR", "").strip()
    if explicit:
        return explicit
    if root in _author_cache:
        return _author_cache[root]
    name = ""
    try:
        name = subprocess.run(
            ["git", "config", "user.name"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    words = name.split()
    author = "".join(w[0] for w in words).upper() if len(words) > 1 else (words[0] if words else "me")
    _author_cache[root] = author
    return author
