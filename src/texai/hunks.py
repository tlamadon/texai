"""Structured diff hunks, so a single change can be shown and undone on its own.

The unified diff in the chat panel is for reading. This is for acting: each
changed region gets a stable id, the lines it replaced, the lines it produced,
and where it lands in the *new* file — which is what SyncTeX needs to point at
it in the rebuilt PDF.

Rejecting one hunk is a reconstruction, not a patch: walk the opcodes between
the snapshot and the current file, take the new text everywhere except the
rejected regions, and write the result. Deterministic, and it does not care
what order things are rejected in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

__all__ = ["Hunk", "file_hunks", "reconstruct", "word_diff"]


@dataclass(frozen=True)
class Hunk:
    """One changed region of one file."""

    id: str
    file: str
    kind: str  # replace | insert | delete
    before: str
    after: str
    new_start: int  # 1-based line in the new file to anchor on
    new_end: int
    old_start: int
    old_end: int

    def as_dict(self) -> dict[str, Any]:
        before_parts, after_parts = word_diff(self.before, self.after)
        return {
            "id": self.id,
            "file": self.file,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "beforeParts": before_parts,
            "afterParts": after_parts,
            "newStart": self.new_start,
            "newEnd": self.new_end,
            "oldStart": self.old_start,
            "oldEnd": self.old_end,
        }


def _hunk_id(file: str, kind: str, before: str, after: str, new_start: int) -> str:
    """Content-derived id.

    Deliberately not a positional index: rejecting one hunk re-diffs the file
    and renumbers the rest, so an index would silently point at the wrong
    change on the second reject.
    """
    # repr keeps the parts unambiguous without inventing a separator that
    # could appear inside the text itself.
    payload = repr((file, kind, before, after, new_start))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _split(text: str) -> list[str]:
    return text.splitlines(keepends=True)


# Words with their trailing whitespace, so joining the pieces is lossless.
_WORDS = re.compile(r"\S+\s*")


def word_diff(before: str, after: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split two strings into runs, marking which words actually differ.

    Rewriting three words in a wrapped paragraph otherwise strikes out two whole
    lines of near-identical text and leaves the reader to spot the difference.
    Returned as ``[{"text": ..., "changed": bool}, ...]`` for each side.
    """
    old = _WORDS.findall(before)
    new = _WORDS.findall(after)

    before_parts: list[dict[str, Any]] = []
    after_parts: list[dict[str, Any]] = []

    def push(parts: list[dict[str, Any]], text: str, changed: bool) -> None:
        if not text:
            return
        if parts and parts[-1]["changed"] == changed:
            parts[-1]["text"] += text
        else:
            parts.append({"text": text, "changed": changed})

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new).get_opcodes():
        push(before_parts, "".join(old[i1:i2]), tag != "equal")
        push(after_parts, "".join(new[j1:j2]), tag != "equal")

    return before_parts, after_parts


def file_hunks(file: str, before_text: str, after_text: str) -> list[Hunk]:
    """Every changed region between two versions of one file."""
    before_lines = _split(before_text)
    after_lines = _split(after_text)
    hunks: list[Hunk] = []

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, before_lines, after_lines).get_opcodes():
        if tag == "equal":
            continue
        before = "".join(before_lines[i1:i2])
        after = "".join(after_lines[j1:j2])
        # A deletion produces no new lines; anchor it on the line that now sits
        # where the removed text used to be.
        new_start = j1 + 1 if j2 > j1 else max(1, j1)
        new_end = j2 if j2 > j1 else new_start
        hunks.append(
            Hunk(
                id=_hunk_id(file, tag, before, after, new_start),
                file=file,
                kind=tag,
                before=before,
                after=after,
                new_start=new_start,
                new_end=new_end,
                old_start=i1 + 1,
                old_end=i2,
            )
        )
    return hunks


def reconstruct(before_text: str, after_text: str, rejected_ids: Iterable[str], file: str) -> str:
    """The new file with the rejected hunks rolled back to their old text."""
    rejected = set(rejected_ids)
    if not rejected:
        return after_text

    before_lines = _split(before_text)
    after_lines = _split(after_text)
    out: list[str] = []

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, before_lines, after_lines).get_opcodes():
        if tag == "equal":
            out.extend(after_lines[j1:j2])
            continue
        before = "".join(before_lines[i1:i2])
        after = "".join(after_lines[j1:j2])
        new_start = j1 + 1 if j2 > j1 else max(1, j1)
        hunk_id = _hunk_id(file, tag, before, after, new_start)
        out.extend(before_lines[i1:i2] if hunk_id in rejected else after_lines[j1:j2])

    return "".join(out)
