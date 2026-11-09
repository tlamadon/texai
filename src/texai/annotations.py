"""Author annotations, kept in the LaTeX source as comment lines.

A line whose first non-blank characters are ``%@`` is an annotation: TeX skips
it like any other comment, so the paper compiles exactly as before, but texai
reads it back and shows it beside the PDF. Living in the source means they
travel with the paper through git, survive any editor, and are read by the agent
along with the prose they describe. Every kind shares one grammar::

    %@kind [fields] [-- text]
    %> continuation lines, when there is more to say than fits on one

The word glued to the ``@`` is the kind (a bare ``%@`` is a comment). Fields are
``key:value`` words (``key:"two words"`` when the value has spaces), and any
annotation can carry these:

* ``id:c4`` — a name, for others to point at;
* ``by:TL`` — who wrote it (the agent signs ``by:agent``);
* ``re:c4`` — a reply to annotation ``c4``;
* ``status:done`` — settled: a todo done, a question answered.

What else a kind takes, and where it is drawn, is in :data:`KINDS`:

    %@intent motivation, contrast with prior work
    %@comment id:c4 by:TL Is this consistent with Table 2?
    %@todo status:done cite the 2019 replication
    %@src @card2018 p.14 "an elasticity of around 0.3" sha:1a2b3c4d
    %@propose id:p1 by:agent -- shorter
    %> We find an elasticity of 0.3.

An annotation is about what follows it: an ``intent`` about its paragraph (the
blank-line-separated block, or the next one down if it stands alone), anything
else about the line below. The paper is written one sentence per line, so that
line is a sentence.

For free-text kinds (``intent``, ``comment``, ``todo``, any unknown one), fields
are read only from the front, and everything after them is the text, exactly as
written — so ``%@comment Note: see Table 2 -- TL`` loses nothing. Structured
kinds (``src``, ``propose``) read every word as a field or an argument, until a
``--`` that starts the free text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "INTENT",
    "PROPOSE",
    "SOURCE",
    "Annotation",
    "Kind",
    "KINDS",
    "kind_of",
    "is_annotation_line",
    "parse_annotations",
    "parse_body",
]

INTENT = "intent"
SOURCE = "src"
PROPOSE = "propose"
DEFAULT_KIND = "comment"

# Fields every kind takes.
GENERIC_KEYS = frozenset({"id", "by", "re", "status"})


@dataclass(frozen=True)
class Kind:
    """What one kind of annotation is, and where it goes.

    ``place`` is what the page does with it, and all the browser looks at:
    ``intent`` is a label in the left margin; ``note`` a slip in the right;
    ``marker`` a small icon at the end of the line; ``proposal`` a set of
    versions of the line below, to pick from.
    """

    place: str
    structured: bool = False  # arguments and fields throughout, free text after `--`
    keys: frozenset[str] = frozenset()  # fields beyond the generic ones


NOTE = Kind("note")
KINDS: Mapping[str, Kind] = MappingProxyType(
    {
        INTENT: Kind("intent"),
        "comment": NOTE,
        "todo": NOTE,
        SOURCE: Kind("marker", structured=True, keys=frozenset({"p", "sha"})),
        PROPOSE: Kind("proposal", structured=True, keys=frozenset({"lines"})),
    }
)


def kind_of(name: str) -> Kind:
    """Any kind not in the table is a note: ``%@question``, ``%@idea``..."""
    return KINDS.get(name, NOTE)


# ``%@kind text``, ``%@kind: text`` or ``%@ text``. The kind must touch the ``@``,
# so ``%@ Fix this`` is a comment that says "Fix this", not a kind called "Fix".
# Must agree with ANNOTATION in editor.js, which tints these lines in the source.
_ANNOTATION = re.compile(r"^\s*%@(?P<kind>[A-Za-z][\w-]*)?:?(?P<text>.*)$")
# A continuation: the rest of the annotation above it.
_CONTINUATION = re.compile(r"^\s*%>(?: ?)(?P<text>.*)$")

# One word of a body: a quoted passage, or a run of anything else, which may
# end in a quoted value (``by:"Thibaut L"``).
_TOKEN = re.compile(
    r'(?P<quote>"[^"]*"|“[^”]*”)|(?P<word>[^\s"“]+(?:"[^"]*")?)'
)
_FIELD = re.compile(r"^(?P<key>[a-z]+):(?P<value>.+)$")
# ``p.14`` reads better than ``p:14`` in a citation, so both mean a page.
_PAGE = re.compile(r"^pp?\.(?P<value>\d+)$")


@dataclass(frozen=True)
class Annotation:
    line: int  # 1-based, as SyncTeX and the editor count: the ``%@`` line
    kind: str  # lower-cased
    text: str = ""  # the free text; continuation lines joined with newlines
    end: int = 0  # the last line it spans, continuations included
    id: str = ""
    by: str = ""
    re: str = ""
    status: str = ""
    args: tuple[str, ...] = ()  # a structured kind's positional words
    quote: str = ""  # a structured kind's quoted passage
    fields: Mapping[str, str] = field(default_factory=dict)  # its own fields
    payload: tuple[str, ...] = ()  # the continuation lines, as written

    def __post_init__(self) -> None:
        if not self.end:
            object.__setattr__(self, "end", self.line)

    @property
    def place(self) -> str:
        return kind_of(self.kind).place

    def as_dict(self) -> dict[str, Any]:
        """What the browser needs: the common fields, and only the ones set."""
        out: dict[str, Any] = {"line": self.line, "end": self.end, "kind": self.kind}
        out["place"] = self.place
        out["text"] = self.text
        for name in ("id", "by", "re", "status", "quote"):
            value = getattr(self, name)
            if value:
                out[name] = value
        return out


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] in "\"“" and value[-1] in "\"”":
        return value[1:-1]
    return value


def parse_body(kind: str, body: str) -> dict[str, Any]:
    """The parts of an annotation's text after ``%@kind``, per :data:`KINDS`.

    Returns ``text``, ``args``, ``quote``, the generic fields and ``fields`` (the
    kind's own), ready to build an :class:`Annotation` from.
    """
    spec = kind_of(kind)
    keys = GENERIC_KEYS | spec.keys
    generic: dict[str, str] = {}
    own: dict[str, str] = {}
    args: list[str] = []
    quote = ""
    text = ""

    def take(word: str) -> bool:
        """File a word as a field, if it is one this kind knows."""
        if "p" in spec.keys and (page := _PAGE.match(word)):
            own["p"] = page.group("value")
            return True
        match = _FIELD.match(word)
        if match is None or match.group("key") not in keys:
            return False
        value = _unquote(match.group("value"))
        (generic if match.group("key") in GENERIC_KEYS else own)[match.group("key")] = value
        return True

    for token in _TOKEN.finditer(body):
        word = token.group(0)
        if word == "--" or (word.startswith("--") and not spec.structured):
            text = body[token.start() + 2 :].strip()
            break
        if spec.structured:
            if token.group("quote") is not None:
                if not quote:
                    quote = _unquote(word).strip()
                continue
            if not take(word):
                args.append(word)
        elif not take(word):
            # Free text from here on, exactly as written.
            text = body[token.start() :].strip()
            break

    return {
        "text": text,
        "args": tuple(args),
        "quote": quote,
        "fields": own,
        **{name: generic.get(name, "") for name in GENERIC_KEYS},
    }


def is_annotation_line(line: str) -> bool:
    """Whether a source line is part of an annotation: a ``%@`` or a ``%>``."""
    return bool(_ANNOTATION.match(line) or _CONTINUATION.match(line))


def parse_annotations(text: str) -> list[Annotation]:
    """Every annotation in a file, top to bottom. Empty ones are dropped.

    A ``%>`` line belongs to the annotation directly above it; one with none
    above is an ordinary comment.
    """
    found: list[Annotation] = []
    lines = text.split("\n")
    number = 0
    while number < len(lines):
        line = lines[number]
        number += 1
        match = _ANNOTATION.match(line)
        if match is None:
            continue
        start = number
        payload: list[str] = []
        while number < len(lines) and (more := _CONTINUATION.match(lines[number])):
            payload.append(more.group("text"))
            number += 1

        kind = (match.group("kind") or DEFAULT_KIND).lower()
        parts = parse_body(kind, match.group("text").strip())
        if not kind_of(kind).structured and payload:
            # A free-text annotation that ran on: one text, line breaks kept.
            parts["text"] = "\n".join([parts["text"], *payload]).strip()
        if not (parts["text"] or parts["args"] or parts["quote"] or payload):
            continue
        found.append(
            Annotation(
                line=start,
                kind=kind,
                end=start + len(payload),
                payload=tuple(payload),
                **parts,
            )
        )
    return found
