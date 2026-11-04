"""Author annotations, kept in the LaTeX source as comment lines.

A line whose first non-blank characters are ``%@`` is an annotation: TeX skips
it like any other comment, so the paper compiles exactly as before, but texai
reads it back and shows it in the margin of the PDF preview. The word glued to
the ``@`` says what kind it is::

    %@intent motivation, contrast with prior work
    %@comment Is this consistent with Table 2? -- TL
    %@todo cite the 2019 replication
    %@ a bare one is a comment

``intent`` is special: a few keywords naming what the paragraph is *for*, drawn
beside the paragraph's colour bar in the left margin. Every other kind is a note,
drawn in the right margin next to the line that follows it. Living in the source
means they travel with the paper through git, survive any editor, and are read
by the agent along with the prose they describe.

An annotation belongs to the paragraph it sits in — the same blank-line-separated
block the paragraph guide colours. A block of nothing but annotations, set off by
blank lines, belongs to the next paragraph down.
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = ["INTENT", "Annotation", "parse_annotations"]

INTENT = "intent"
DEFAULT_KIND = "comment"

# ``%@kind text``, ``%@kind: text`` or ``%@ text``. The kind must touch the ``@``,
# so ``%@ Fix this`` is a comment that says "Fix this", not a kind called "Fix".
# Must agree with ANNOTATION in editor.js, which tints these lines in the source.
_ANNOTATION = re.compile(r"^\s*%@(?P<kind>[A-Za-z][\w-]*)?:?(?P<text>.*)$")


class Annotation(NamedTuple):
    line: int  # 1-based, as SyncTeX and the editor count
    kind: str  # lower-cased; ``intent`` or any note kind
    text: str


def parse_annotations(text: str) -> list[Annotation]:
    """Every annotation in a file, top to bottom. Empty ones are dropped."""
    found: list[Annotation] = []
    for number, line in enumerate(text.split("\n"), start=1):
        match = _ANNOTATION.match(line)
        if match is None:
            continue
        body = match.group("text").strip()
        if not body:
            continue
        kind = (match.group("kind") or DEFAULT_KIND).lower()
        found.append(Annotation(number, kind, body))
    return found
