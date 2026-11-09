"""Edit proposals, kept in the source until someone decides.

A ``%@propose`` annotation is one version of the lines below it; the version's
text is its ``%>`` continuation lines, and its label the free text::

    %@propose id:p1 by:agent -- shorter
    %> We find an elasticity of 0.3.
    %@propose id:p1 by:agent -- with the range
    %> We find an elasticity of 0.3 (0.25 to 0.36 across specifications).
    We find that the elasticity, across all of our specifications, is about 0.3.

Consecutive proposals with the same ``id`` are one choice. They sit directly
above the lines they would replace — one, the sentence below, unless
``lines:N`` says more — so nothing else may come between them and their target.
Picking a version puts its lines in place of the target and removes the whole
choice; dismissing removes the choice and keeps the text.

Living in the file, they survive a restart, travel through git to a co-author,
and can be left for next week. The agent's ``offer_options`` writes them (see
:mod:`texai.options`); a person can write them by hand just as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .annotations import PROPOSE, Annotation, parse_annotations

__all__ = [
    "Proposal",
    "ProposalError",
    "apply_proposal",
    "dismiss_proposal",
    "find_proposal",
    "insert_proposal",
    "proposals_in",
]


class ProposalError(ValueError):
    """No such choice in the file, or not one that can be applied."""


@dataclass(frozen=True)
class Proposal:
    """One choice: its versions, and the lines they would replace."""

    id: str
    line: int  # first %@propose line
    end: int  # last line of the last version
    start: int  # first line of the target
    stop: int  # last line of the target
    options: tuple[dict[str, str], ...]  # {label, by, text}
    original: str  # the target lines, as they are now

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "line": self.line,
            "end": self.end,
            "start": self.start,
            "stop": self.stop,
            "options": [dict(option) for option in self.options],
            "original": self.original,
        }


def _target_size(annotation: Annotation) -> int:
    value = annotation.fields.get("lines", "")
    return max(1, int(value)) if value.isdigit() else 1


def proposals_in(text: str, annotations: list[Annotation] | None = None) -> list[Proposal]:
    """Every choice in a file, top to bottom."""
    lines = text.split("\n")
    found = [a for a in (annotations or parse_annotations(text)) if a.kind == PROPOSE]
    groups: list[list[Annotation]] = []
    for annotation in found:
        last = groups[-1][-1] if groups else None
        if last is not None and annotation.line == last.end + 1 and annotation.id == last.id:
            groups[-1].append(annotation)
        else:
            groups.append([annotation])

    choices: list[Proposal] = []
    for group in groups:
        size = max(_target_size(a) for a in group)
        start = group[-1].end + 1
        stop = min(start + size - 1, len(lines))
        if start > len(lines):
            continue  # nothing below to replace
        options = tuple(
            {"label": a.text or f"Version {i + 1}", "by": a.by, "text": "\n".join(a.payload)}
            for i, a in enumerate(group)
        )
        choices.append(
            Proposal(
                id=group[0].id,
                line=group[0].line,
                end=group[-1].end,
                start=start,
                stop=stop,
                options=options,
                original="\n".join(lines[start - 1 : stop]),
            )
        )
    return choices


def find_proposal(text: str, proposal_id: str, line: int | None = None) -> Proposal:
    """A choice by its id or, for one written without an id, by its first line."""
    for proposal in proposals_in(text):
        if proposal_id and proposal.id == proposal_id:
            return proposal
        if not proposal_id and line is not None and proposal.line == line:
            return proposal
    raise ProposalError(f"No proposal {proposal_id or f'at line {line}'} in the file.")


def apply_proposal(text: str, proposal: Proposal, index: int) -> str:
    """The file with version ``index`` in place of the target, and the choice gone."""
    if not 0 <= index < len(proposal.options):
        raise ProposalError(f"Proposal {proposal.id} has no version {index + 1}.")
    lines = text.split("\n")
    replacement = proposal.options[index]["text"].split("\n")
    return "\n".join(lines[: proposal.line - 1] + replacement + lines[proposal.stop :])


def dismiss_proposal(text: str, proposal: Proposal) -> str:
    """The file with the choice removed and the text left as it is."""
    lines = text.split("\n")
    return "\n".join(lines[: proposal.line - 1] + lines[proposal.end :])


_ID = re.compile(r"\bid:p(\d+)\b")


def _free_id(text: str) -> str:
    taken = {int(match.group(1)) for match in _ID.finditer(text)}
    number = 1
    while number in taken:
        number += 1
    return f"p{number}"


def _field(value: str) -> str:
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def insert_proposal(
    text: str, start: int, stop: int, options: list[dict[str, str]], by: str = "agent"
) -> tuple[str, str, int]:
    """Write a choice above lines ``start``-``stop``.

    Each option's ``text`` is the full replacement for those lines. Returns the
    new file, the id given to the choice, and where the target now starts.
    """
    lines = text.split("\n")
    target = lines[start - 1]
    indent = target[: len(target) - len(target.lstrip())]
    proposal_id = _free_id(text)
    size = stop - start + 1

    block: list[str] = []
    for option in options:
        head = [f"{indent}%@propose", f"id:{proposal_id}"]
        if by:
            head.append(f"by:{_field(by)}")
        if size > 1:
            head.append(f"lines:{size}")
        label = " ".join(str(option.get("label") or "").split())
        if label:
            head += ["--", label]
        block.append(" ".join(head))
        block += [f"{indent}%> {line}".rstrip() for line in option["text"].split("\n")]

    new = lines[: start - 1] + block + lines[start - 1 :]
    return "\n".join(new), proposal_id, start + len(block)
