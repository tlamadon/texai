"""The agent's tools for annotation records: read them, add a source, comment.

Records live in ``annotations.jsonl`` and their markers on the sentences (see
:mod:`texai.records`). The agent could edit both by hand, but a marker typed
wrong or a quote copied loosely breaks what the reader sees, so it goes through
these instead: texai writes the marker and the record, and checks a source as
it goes — the label exists, the file is there, the quote is in the paper word
for word — and stamps the page and hash itself.

Each returns its answer as text, the way the agent reads it. Both agents reach
them: Claude in-process, Codex over texai's MCP endpoint.
"""

from __future__ import annotations

import json
from typing import Any

from .config import AppConfig
from .papers import _find, _locate, _page_texts
from .provenance import (
    ProvenanceError,
    _find_label,
    file_sha,
    parse_source,
    serve_path,
)
from .records import (
    RecordError,
    add_comment,
    add_marker,
    load_records,
    markers_in,
    reply_to,
    save_records,
    sentence_line,
)
from .annotations import is_annotation_line
from .selection import atomic_write_text
from .source import SourceError, read_source

__all__ = [
    "ADD_SOURCE_DESCRIPTION",
    "ANNOTATIONS_DESCRIPTION",
    "COMMENT_DESCRIPTION",
    "add_source",
    "annotations_for",
    "comment",
]

ANNOTATIONS_DESCRIPTION = """
What is attached to the sentences of a passage: their sources and comments,
kept in annotations.jsonl and marked in the source by `%#id` at the end of a
sentence's line. `file` is the .tex; `line` the sentence, or the first of a
range ending at `to_line`. Use it before rewriting sourced sentences, and to
check or discuss their sources.
""".strip()

ADD_SOURCE_DESCRIPTION = """
Record what backs a sentence. `line` is the sentence's line in `file`.
`target` is `@key` for papers/<key>.pdf, `#label` for something in this paper,
or a project path (with `:120-145` for lines). For a paper, give `quote`: the
words that back the claim, as the PDF has them (find them with read_paper). The
quote is checked against the PDF and the page and hash are filled in; a quote
that is not there is refused with the closest match. `note` is optional.
""".strip()

COMMENT_DESCRIPTION = """
Leave a comment on a sentence (`file`, `line`), or answer one: `reply_to` is
the comment's id as `annotations` lists it (`k3f9q:c1`). Signed as the agent.
""".strip()


def _source_text(config: AppConfig, file: str) -> tuple[str, str]:
    try:
        source = read_source(config, file)
    except SourceError as exc:
        raise RecordError(str(exc)) from exc
    return source["file"], source["text"]


def _sentence_check(text: str, line: int) -> None:
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        raise RecordError(f"There is no line {line}; the file has {len(lines)}.")
    target = lines[line - 1]
    if not target.strip() or is_annotation_line(target) or target.lstrip().startswith("%"):
        raise RecordError(f"Line {line} is not a sentence: name the line the sentence is on.")


def annotations_for(config: AppConfig, file: str, line: int, to_line: int | None = None) -> str:
    """The body of ``annotations`` for every agent."""
    try:
        relative, text = _source_text(config, file)
    except RecordError as exc:
        return f"Could not read that: {exc}"
    first, last = int(line), int(to_line or line)
    lines = text.split("\n")
    records = load_records(config)
    out = []
    for marker, record_id, alone in markers_in(text):
        about = sentence_line(lines, marker, alone)
        if not first <= about <= last:
            continue
        record = records.get(record_id)
        if record is None:
            out.append(f"{relative}:{about} %#{record_id} — no such record in annotations.jsonl")
            continue
        out.append(f"{relative}:{about} %#{record_id}")
        for source in record.get("sources", []):
            out.append("  source " + json.dumps(source, ensure_ascii=False))
        for comment_entry in record.get("comments", []):
            state = f" [{comment_entry['status']}]" if comment_entry.get("status") else ""
            out.append(
                f"  comment {record_id}:{comment_entry.get('id')}{state} "
                f"{comment_entry.get('by', '?')}: {comment_entry.get('text', '')}"
            )
            for reply in comment_entry.get("replies", []):
                out.append(f"    reply {record_id}:{reply.get('id')} {reply.get('by', '?')}: {reply.get('text', '')}")
    if not out:
        span = f"{first}" if first == last else f"{first}-{last}"
        return f"Nothing is attached to {relative}:{span}."
    return "\n".join(out)


def _checked_source(config: AppConfig, target: str, page: Any, quote: str, note: str) -> tuple[dict[str, Any], str]:
    """The source as it will be recorded, and a word on what was checked."""
    ref = parse_source(target)
    if ref is None:
        raise RecordError("Name the source: @key, #label or a project path.")
    entry: dict[str, Any] = {"type": ref.type, "target": ref.target}
    if ref.start:
        entry["start"], entry["end"] = ref.start, ref.end
    said = ""
    if ref.type == "label":
        if _find_label(config, ref.target) is None:
            raise RecordError(f"There is no \\label{{{ref.target}}} in the paper.")
    else:
        pdf = None
        try:
            if ref.type == "paper" or ref.target.lower().endswith(".pdf"):
                pdf, _key = _locate(config, f"@{ref.target}" if ref.type == "paper" else ref.target)
            else:
                path = serve_path(config, ref.target)
                if not path.is_file():
                    raise RecordError(f"There is no {ref.target} in the project.")
        except ProvenanceError as exc:
            raise RecordError(str(exc)) from exc
        if pdf is not None:
            entry["sha"] = file_sha(pdf)[:8]
            if quote:
                texts = _page_texts(pdf)
                try:
                    found = _find(texts, quote)
                except ProvenanceError as exc:
                    raise RecordError(str(exc)) from exc
                if found is None:
                    raise RecordError("That quote is not in the PDF. Find it with read_paper and quote it as the PDF has it.")
                if not found["exact"]:
                    raise RecordError(
                        f"That quote is not in the PDF word for word. Closest, on page {found['page']}: "
                        f"«{found['match']}». Quote it as the PDF has it."
                    )
                entry["page"] = found["page"]
                if page and int(page) != found["page"]:
                    said = f" (on page {found['page']}, not {page})"
    if quote:
        entry["quote"] = " ".join(quote.split())
    if page and "page" not in entry:
        entry["page"] = int(page)
    if note:
        entry["note"] = " ".join(note.split())
    return entry, said


def add_source(
    config: AppConfig, file: str, line: int, target: str, page: Any = None, quote: str = "", note: str = ""
) -> str:
    """The body of ``add_source`` for every agent."""
    try:
        relative, text = _source_text(config, file)
        line = int(line)
        _sentence_check(text, line)
        entry, said = _checked_source(config, str(target or "").strip(), page, str(quote or ""), str(note or ""))
        records = json.loads(json.dumps(load_records(config)))
        taken = set(records) | {record for _, record, _ in markers_in(text)}
        new_text, record_id = add_marker(text, line, taken)
        record = records.setdefault(record_id, {"id": record_id})
        sources = record.setdefault("sources", [])
        if entry in sources:
            return f"{relative}:{line} already has that source (record %#{record_id})."
        sources.append(entry)
        save_records(config, records)
        if new_text != text:
            atomic_write_text(config.root / relative, new_text)
    except (RecordError, ValueError) as exc:
        return f"Not added: {exc}"
    shown = {"label": "#", "paper": "@"}.get(entry["type"], "") + entry["target"]
    page_part = f" p.{entry['page']}" if entry.get("page") else ""
    return f"Added source {shown}{page_part}{said} to {relative}:{line} (record %#{record_id})."


def comment(config: AppConfig, file: str, line: int, text: str, reply_to_id: str = "") -> str:
    """The body of ``comment`` for every agent."""
    try:
        relative, source = _source_text(config, file)
        records = json.loads(json.dumps(load_records(config)))
        if reply_to_id:
            record_id, _, comment_id = str(reply_to_id).partition(":")
            record = records.get(record_id.lstrip("%#"))
            if record is None or not comment_id:
                raise RecordError(f"No comment {reply_to_id}; give it as annotations lists it, record:comment.")
            reply_to(record, comment_id, text, "agent")
            save_records(config, records)
            return f"Replied to {reply_to_id}."
        line = int(line)
        _sentence_check(source, line)
        taken = set(records) | {record for _, record, _ in markers_in(source)}
        new_source, record_id = add_marker(source, line, taken)
        added = add_comment(records.setdefault(record_id, {"id": record_id}), text, "agent")
        save_records(config, records)
        if new_source != source:
            atomic_write_text(config.root / relative, new_source)
    except (RecordError, ValueError) as exc:
        return f"Not added: {exc}"
    return f"Commented on {relative}:{line} as {record_id}:{added['id']}."

