"""Three-way merge, so an outside edit and an unsaved one can both survive.

The editor and the agent write the same files, so every save carries the hash
of the text it was based on and a mismatch means the file moved underneath.
But "moved" almost always means some other paragraph changed. Refusing the
save is safe and useless; reloading over the buffer is worse. Either way the
user is told to sort out a collision that never happened.

So: diff the base against both sides and, as long as no two changes touch the
same lines, take them all. Changes that do overlap are a real conflict and are
reported as one — the editor keeps its buffer and says so.

The result is handed back as splices into *ours* rather than as a new document:
the editor applies them to its buffer, which keeps the cursor, the undo history
and the scroll position, and marks exactly the lines that arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

__all__ = ["Merge", "MergeEdit", "merge3", "normalize_newlines"]

# A change to a region of the base: replace lines [start, end) with these.
_Change = tuple[int, int, list[str]]


def normalize_newlines(text: str) -> str:
    """CRLF and lone CR to LF — the only line ending the buffer ever holds."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _lines(text: str) -> list[str]:
    """Lines with their newline attached, split where the editor splits.

    Not ``str.splitlines``: that also breaks on form feeds and the Unicode
    separators, which CodeMirror keeps inside a line. A split it disagreed with
    would put the offsets it is sent one line out.
    """
    parts = text.split("\n")
    tail = parts.pop()
    lines = [part + "\n" for part in parts]
    if tail:
        lines.append(tail)
    return lines


@dataclass(frozen=True)
class MergeEdit:
    """One splice into ``ours``: replace the characters in [start, end)."""

    start: int
    end: int
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True)
class Merge:
    """What came of folding the other side in."""

    clean: bool  # False when the two sides changed the same lines
    text: str  # the merged document, or ``ours`` untouched when not clean
    edits: tuple[MergeEdit, ...]  # ours -> text, empty when nothing arrived


def merge3(base: str, ours: str, theirs: str) -> Merge:
    """Fold ``theirs`` into ``ours``, both being edits of ``base``."""
    base, ours, theirs = (normalize_newlines(text) for text in (base, ours, theirs))
    # Nothing came in, or it is already here.
    if base == theirs or ours == theirs:
        return Merge(True, ours, ())
    # Nothing local to protect: their version wins outright.
    if base == ours:
        return _spliced(ours, theirs)

    woven = _weave(_lines(base), _lines(ours), _lines(theirs))
    if woven is None:
        return Merge(False, ours, ())
    return _spliced(ours, "".join(woven))


def _changes(base: list[str], other: list[str]) -> list[_Change]:
    """Every region of ``base`` that ``other`` rewrote.

    ``autojunk`` is off: it writes off lines that recur more than a hundred
    times in a long file, and in LaTeX that is the blank line between every
    paragraph. Treating those as noise smears small edits into large ones,
    and a large one is far likelier to be called a conflict.
    """
    matcher = SequenceMatcher(None, base, other, autojunk=False)
    return [
        (i1, i2, other[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _clashes(a: _Change, b: _Change) -> bool:
    """Whether two changes cannot be applied independently of each other.

    Sharing a starting line counts, even when neither replaces any of the
    other's lines: two insertions at one point have no order between them, and
    picking one is guesswork.
    """
    if a[0] == b[0]:
        return True
    return a[0] < b[1] and b[0] < a[1]


def _weave(base: list[str], ours: list[str], theirs: list[str]) -> list[str] | None:
    """The base with both sides' changes applied, or ``None`` if they collide."""
    mine = _changes(base, ours)
    yours = _changes(base, theirs)

    out: list[str] = []
    at = 0  # how far through the base the output has been carried
    o = t = 0

    while o < len(mine) or t < len(yours):
        a = mine[o] if o < len(mine) else None
        b = yours[t] if t < len(yours) else None

        if a is not None and b is not None:
            if a == b:  # both sides made the same edit; take it once
                out.extend(base[at : a[0]])
                out.extend(a[2])
                at = a[1]
                o += 1
                t += 1
                continue
            if _clashes(a, b):
                return None

        # Whichever comes first in the base. The one left behind is checked
        # against the next change on this side before anything else is taken,
        # so an overlap two changes deep is still caught.
        take = a if b is None or (a is not None and a[0] < b[0]) else b
        out.extend(base[at : take[0]])
        out.extend(take[2])
        at = take[1]
        if take is a:
            o += 1
        else:
            t += 1

    out.extend(base[at:])
    return out


def _spliced(ours: str, merged: str) -> Merge:
    """The merge, with the difference expressed as character splices into ours."""
    old, new = _lines(ours), _lines(merged)

    starts: list[int] = []
    at = 0
    for line in old:
        starts.append(at)
        at += len(line)
    starts.append(at)  # so a hunk running to the end has an offset to stop at

    edits = tuple(
        MergeEdit(starts[i1], starts[i2], "".join(new[j1:j2]))
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        if tag != "equal"
    )
    return Merge(True, merged, edits)
