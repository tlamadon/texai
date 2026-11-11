"""Annotation records: what is attached to a sentence, kept beside the paper.

A sentence's sources and comments can outgrow the comment lines above it — a
claim backed by five papers, each with a quote and a page, and a conversation
about it. So they can live in ``annotations.jsonl``, next to the root ``.tex``,
one record per line, and the sentence carries only a marker naming its record::

    We find a large elasticity. %#k3f9q

    {"id": "k3f9q", "sources": [{"type": "paper", "target": "card2018", ...}],
     "comments": [{"id": "c1", "kind": "comment", "by": "TL", "text": "...",
                   "replies": [{"id": "c2", "by": "agent", "text": "..."}]}]}

The marker is what anchors the record: it is part of the sentence's line, so
any edit by anyone — the agent, an editor, a merge — carries it along. It sits
at the end of the line, after a space (so TeX still ends the sentence with
one), and adding it shifts no lines: what SyncTeX knows stays true, and a new
comment needs no rebuild. A marker alone on its line is about the next line of
text, like the ``%@`` annotations.

IDs are random, so two people adding records on two branches do not collide,
and the file is sorted and one record a line, so git merges it like prose.
The inline ``%@src`` and ``%@comment`` forms stay valid beside all this; see
:func:`externalize` for moving them out.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from .annotations import PROPOSE, SOURCE, INTENT, is_annotation_line, kind_of, parse_annotations
from .config import AppConfig
from .provenance import source_ref
from .selection import atomic_write_text

__all__ = [
    "RECORDS_FILE",
    "RecordError",
    "add_comment",
    "add_marker",
    "apply_comment",
    "attached_summary",
    "delete_comment",
    "externalize",
    "load_records",
    "markers_in",
    "new_id",
    "record_at",
    "records_path",
    "remove_marker",
    "reply_to",
    "save_records",
    "set_comment_status",
    "sentence_line",
]

RECORDS_FILE = "annotations.jsonl"

# ``%#k3f9q``: not escaped (``\%#`` is text), anywhere on a line.
MARKER = re.compile(r"(?<!\\)%#(?P<id>[a-z0-9]{4,16})\b")
# A marker with the space that sets it off from its sentence.
_MARKER_AT_END = re.compile(r"\s*(?<!\\)%#[a-z0-9]{4,16}\b")
_MARKER_ONLY = re.compile(r"^\s*%#[a-z0-9]{4,16}\s*$")
# No look-alikes: an ID is read and typed by people.
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
_ID_LENGTH = 6


class RecordError(ValueError):
    """A record or marker that is not where, or not what, it should be."""


def records_path(config: AppConfig) -> Path:
    """``annotations.jsonl`` beside the root ``.tex``, or at the project root."""
    base = config.root_tex.parent if config.root_tex else config.root
    return base / RECORDS_FILE


# ------------------------------------------------------------------ the file

_cache: dict[Path, tuple[int, int, dict[str, dict[str, Any]]]] = {}


def load_records(config: AppConfig) -> dict[str, dict[str, Any]]:
    """Every record by ID. A line that is not a record is skipped, not fatal:
    a half-resolved merge should still show what it can."""
    path = records_path(config)
    try:
        stat = path.stat()
    except OSError:
        return {}
    cached = _cache.get(path)
    if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
        return cached[2]
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            records[record["id"]] = record
    _cache[path] = (stat.st_size, stat.st_mtime_ns, records)
    return records


def _empty(record: dict[str, Any]) -> bool:
    return not any(record.get(key) for key in ("sources", "comments"))


def save_records(config: AppConfig, records: dict[str, dict[str, Any]]) -> None:
    """Write every non-empty record, one a line, sorted by ID."""
    lines = []
    for record_id in sorted(records):
        record = records[record_id]
        if _empty(record):
            continue
        ordered = {"id": record_id, **{k: v for k, v in record.items() if k != "id" and v != []}}
        lines.append(json.dumps(ordered, ensure_ascii=False))
    path = records_path(config)
    if not lines and not path.exists():
        return
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def new_id(taken: set[str]) -> str:
    while True:
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(_ID_LENGTH))
        if candidate not in taken:
            return candidate


# ------------------------------------------------------------------ markers


def changes_no_text(before: str, after: str) -> bool:
    """Whether going from ``before`` to ``after`` leaves the document's text as
    it was: only ``%@`` annotations, their ``%>`` lines and ``%#`` record
    markers came, went or changed.

    Such an edit — a source added to a sentence, a comment answered — puts
    nothing different on the page, so there is nothing in it to review.
    """

    def text(block: str) -> list[str]:
        return [
            _MARKER_AT_END.sub("", line).rstrip()
            for line in block.splitlines()
            if not is_annotation_line(line)
        ]

    return text(before) == text(after)


def fold_annotations(before: str, after: str) -> str | None:
    """``before`` with the annotations of ``after`` and none of its new text,
    or ``None`` when the two cannot be told apart line for line.

    For a change that does both — a sentence reworded and a source attached to
    the one above it — this is what the review should start from: the sources
    and markers in place, the old wording still to be accepted or rejected.
    """
    old = [line for line in before.splitlines(keepends=True) if not is_annotation_line(line)]
    new = after.splitlines(keepends=True)
    if len(old) != sum(1 for line in new if not is_annotation_line(line)):
        return None

    def bare(line: str) -> str:
        return _MARKER_AT_END.sub("", line).rstrip()

    out: list[str] = []
    at = 0
    for line in new:
        if is_annotation_line(line):
            out.append(line)
            continue
        # The same sentence, with or without its marker: take it as it now is.
        out.append(line if bare(old[at]) == bare(line) else old[at])
        at += 1
    folded = "".join(out)
    return None if folded == before else folded


def markers_in(text: str) -> list[tuple[int, str, bool]]:
    """``(line, id, alone)`` for every marker, ``alone`` when it has the line
    to itself."""
    found = []
    for number, line in enumerate(text.split("\n"), start=1):
        for match in MARKER.finditer(line):
            found.append((number, match.group("id"), bool(_MARKER_ONLY.match(line))))
    return found


def is_marker_line(line: str) -> bool:
    return bool(_MARKER_ONLY.match(line))


def sentence_line(lines: list[str], line: int, alone: bool) -> int:
    """The line a marker is about: its own, or for one alone on its line, the
    next line of text in the paragraph (0 when there is none)."""
    if not alone:
        return line
    for number in range(line + 1, len(lines) + 1):
        text = lines[number - 1]
        if not text.strip():
            return 0
        if is_annotation_line(text) or is_marker_line(text):
            continue
        return number
    return 0


def record_at(text: str, line: int) -> str | None:
    """The record on a line of text: its own marker, or one alone above it."""
    lines = text.split("\n")
    for number, record_id, alone in markers_in(text):
        if sentence_line(lines, number, alone) == line:
            return record_id
    return None


def add_marker(text: str, line: int, taken: set[str]) -> tuple[str, str]:
    """The file with a marker at the end of line ``line``, and its ID; the
    line's own marker if it has one. No line moves."""
    existing = record_at(text, line)
    if existing:
        return text, existing
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        raise RecordError(f"There is no line {line} in that file.")
    record_id = new_id(taken)
    lines[line - 1] = f"{lines[line - 1].rstrip()} %#{record_id}"
    return "\n".join(lines), record_id


def remove_marker(text: str, record_id: str) -> tuple[str, bool]:
    """The file without the marker, and whether any line went with it (a
    marker alone on its line takes the line; one after text leaves it)."""
    lines = text.split("\n")
    kept: list[str] = []
    shifted = False
    pattern = re.compile(r"\s*(?<!\\)%#" + re.escape(record_id) + r"\b")
    for line in lines:
        if not pattern.search(line):
            kept.append(line)
        elif _MARKER_ONLY.match(line):
            shifted = True
        else:
            kept.append(pattern.sub("", line).rstrip())
    return "\n".join(kept), shifted


# ------------------------------------------------------------------ comments


def add_comment(record: dict[str, Any], text: str, by: str, kind: str = "comment") -> dict[str, Any]:
    """A new thread on the record."""
    if not text.strip():
        raise RecordError("Write something first.")
    entry = {"id": f"c{_next_comment_number(record)}", "kind": kind, "by": by, "text": text.strip()}
    record.setdefault("comments", []).append({k: v for k, v in entry.items() if v})
    return entry


def _find(record: dict[str, Any], comment_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """A comment, and the thread it replies to (``None`` for a thread itself)."""
    for comment in record.get("comments", []):
        if comment.get("id") == comment_id:
            return comment, None
        for reply in comment.get("replies", []):
            if reply.get("id") == comment_id:
                return reply, comment
    raise RecordError(f"No comment {comment_id} in record {record.get('id')}; it may have been deleted.")


def reply_to(record: dict[str, Any], comment_id: str, text: str, by: str) -> dict[str, Any]:
    """A reply, at the end of the thread the comment is in."""
    if not text.strip():
        raise RecordError("Write something first.")
    comment, thread = _find(record, comment_id)
    root = thread or comment
    entry = {"id": f"c{_next_comment_number(record)}", "by": by, "text": text.strip()}
    root.setdefault("replies", []).append({k: v for k, v in entry.items() if v})
    return entry


def set_comment_status(record: dict[str, Any], comment_id: str, status: str) -> None:
    """Resolve a thread (``done``), or reopen it (empty)."""
    comment, thread = _find(record, comment_id)
    target = thread or comment
    if status:
        target["status"] = status
    else:
        target.pop("status", None)


def delete_comment(record: dict[str, Any], comment_id: str) -> None:
    """A reply alone, or a thread with all its replies."""
    comment, thread = _find(record, comment_id)
    if thread is None:
        record["comments"] = [c for c in record.get("comments", []) if c is not comment]
    else:
        thread["replies"] = [r for r in thread.get("replies", []) if r is not comment]
        if not thread["replies"]:
            thread.pop("replies")


def apply_comment(
    config: AppConfig,
    file: str,
    action: str,
    *,
    line: int = 0,
    text: str = "",
    record_id: str = "",
    comment_id: str = "",
    by: str = "",
) -> dict[str, Any]:
    """Do what the page asked to a comment kept in a record, and save it.

    ``add`` starts a thread on the sentence at ``line``, giving it a marker if
    it has none; the rest act on comment ``comment_id`` of ``record_id``. A
    record left with nothing in it goes, with its marker. Returns whether the
    ``.tex`` changed, and whether any of its lines moved (only when a marker
    that had a line to itself goes).
    """
    path = config.root / file
    source = path.read_text(encoding="utf-8", errors="replace")
    # A copy: the loaded records are cached, and this may fail half way.
    records = json.loads(json.dumps(load_records(config)))
    new_source, shifted = source, False
    if action == "add":
        taken = set(records) | {record for _, record, _ in markers_in(source)}
        new_source, record_id = add_marker(source, line, taken)
        record = records.setdefault(record_id, {"id": record_id})
        added = add_comment(record, text, by)
        comment_id = added["id"]
    else:
        record = records.get(record_id)
        if record is None:
            raise RecordError(f"Record {record_id} is no longer in {RECORDS_FILE}.")
        if action == "reply":
            reply_to(record, comment_id, text, by)
        elif action in ("resolve", "reopen"):
            set_comment_status(record, comment_id, "done" if action == "resolve" else "")
        elif action == "delete":
            delete_comment(record, comment_id)
        else:
            raise RecordError(f"Unknown action {action}.")
        if _empty(record):
            records.pop(record_id)
            new_source, shifted = remove_marker(source, record_id)
    save_records(config, records)
    if new_source != source:
        atomic_write_text(path, new_source)
    return {"record": record_id, "comment": comment_id, "texChanged": new_source != source, "shifted": shifted}


# ------------------------------------------------------------------ for the agent


def attached_summary(config: AppConfig, file: str, line: int) -> str:
    """What is attached to a sentence, in a few lines, for a turn's prompt."""
    try:
        text = (config.root / file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    record_id = record_at(text, line)
    record = load_records(config).get(record_id or "")
    if not record:
        return ""
    parts = [f"record %#{record_id}"]
    for source in record.get("sources", []):
        target = {"label": "#", "paper": "@"}.get(source.get("type"), "") + source.get("target", "")
        bits = [target]
        if source.get("page"):
            bits.append(f"p.{source['page']}")
        if source.get("quote"):
            bits.append(f"“{source['quote']}”")
        if source.get("note"):
            bits.append(f"-- {source['note']}")
        parts.append("source: " + " ".join(bits))
    for comment in record.get("comments", []):
        state = f" [{comment['status']}]" if comment.get("status") else ""
        parts.append(f"{comment.get('kind', 'comment')}{state} {comment.get('by', '?')}: {comment.get('text', '')}")
        for reply in comment.get("replies", []):
            parts.append(f"  reply {reply.get('by', '?')}: {reply.get('text', '')}")
    return "; ".join(parts)


# ------------------------------------------------------------------ moving out


def _next_comment_number(record: dict[str, Any]) -> int:
    """One past the highest ``cN`` in the record, replies included."""
    numbers = [0]
    for comment in record.get("comments", []):
        for entry in (comment, *comment.get("replies", [])):
            match = re.fullmatch(r"c(\d+)", str(entry.get("id", "")))
            if match:
                numbers.append(int(match.group(1)))
    return max(numbers) + 1


def _comment_entry(annotation: Any, number: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": f"c{number}", "kind": annotation.kind, "by": annotation.by, "text": annotation.text}
    if annotation.status:
        entry["status"] = annotation.status
    return {k: v for k, v in entry.items() if v}


def externalize(config: AppConfig, path: Path, records: dict[str, dict[str, Any]]) -> tuple[str, int]:
    """One file with its ``%@src`` and comment-like annotations moved into
    records (``records`` is updated in place). Intents and proposals stay.
    Returns the new text and how many annotations moved."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    annotations = parse_annotations(text)
    movable = [
        a
        for a in annotations
        if a.kind not in (INTENT, PROPOSE)
        and (a.kind == SOURCE or not kind_of(a.kind).structured)
    ]
    if not movable:
        return text, 0

    def about(annotation: Any) -> int:
        """The line of text it is about: the next one, or the last one above."""
        for number in range(annotation.end + 1, len(lines) + 1):
            line = lines[number - 1]
            if not line.strip():
                break
            if is_annotation_line(line) or is_marker_line(line) or line.lstrip().startswith("%"):
                continue
            return number
        for number in range(annotation.line - 1, 0, -1):
            line = lines[number - 1]
            if not line.strip():
                break
            if not (is_annotation_line(line) or line.lstrip().startswith("%")):
                return number
        return 0

    by_line: dict[int, list[Any]] = {}
    for annotation in movable:
        target = about(annotation)
        if target:
            by_line.setdefault(target, []).append(annotation)

    taken = set(records)
    ids = {a.id: a for a in movable if a.id}
    moved: set[int] = set()
    for target, group in by_line.items():
        existing = record_at("\n".join(lines), target)
        record_id = existing or new_id(taken)
        taken.add(record_id)
        record = records.setdefault(record_id, {"id": record_id})
        comments = record.setdefault("comments", [])
        roots: dict[str, dict[str, Any]] = {}
        for annotation in group:
            if annotation.kind == SOURCE:
                ref = source_ref(annotation)
                if ref is None:
                    continue
                entry = {k: v for k, v in ref.as_dict().items() if v not in (None, "")}
                record.setdefault("sources", []).append(entry)
            else:
                # A reply goes under the comment it answers, wherever that
                # comment is in the thread; the rest start threads of their own.
                root_of, seen = annotation, {annotation.id}
                while root_of.re and root_of.re in ids and root_of.re not in seen:
                    seen.add(root_of.re)
                    root_of = ids[root_of.re]
                entry = _comment_entry(annotation, _next_comment_number(record))
                if root_of is annotation or root_of.id not in roots:
                    comments.append(entry)
                    if annotation.id:
                        roots[annotation.id] = entry
                else:
                    roots[root_of.id].setdefault("replies", []).append(entry)
                    if annotation.id:
                        roots[annotation.id] = roots[root_of.id]
            moved.update(range(annotation.line, annotation.end + 1))
        if _empty(record):
            records.pop(record_id, None)
        elif not existing:
            lines[target - 1] = f"{lines[target - 1].rstrip()} %#{record_id}"

    kept = [line for number, line in enumerate(lines, start=1) if number not in moved]
    return "\n".join(kept), sum(len(group) for group in by_line.values())
