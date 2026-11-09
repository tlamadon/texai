"""Quick edits: one marked passage, one request, usually one model call.

Most requests from the page are "change this passage": tighten it, fix this
sentence, three versions of this title. Through the agent such a request costs
three or four model calls — find the passage, read it, edit it, then say what
it did — at about two seconds each, and the speed of the model hardly enters
into it (see texai.timing). Here the passage arrives with the request, with its
line numbers and the lines around it, and the answer is the new text itself, so
a single call does it and texai writes the lines.

The quick editor is a session of its own, apart from the agent's: a short
system prompt, Read and Grep for the times the context shown is not enough (a
definition, the notation, what a reference points to), and nothing that
writes. Anything more than rewriting the passage — other places, other files,
new tables — it hands to the agent, which then gets the request as usual. So
does any answer texai cannot use. The turn around it is the same either way:
snapshot, build, revert, review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .agent import AgentSession
from .config import AppConfig
from .models import SelectionRef
from .paragraphs import paragraph_ranges
from .selection import atomic_write_text
from .source import SourceError, read_source

__all__ = [
    "Passage",
    "QuickAnswer",
    "QuickSession",
    "QuickStale",
    "apply_edit",
    "compose_quick_prompt",
    "find_passage",
    "parse_answer",
    "quick_eligible",
]

# Read-only: the answer is the edit, and texai makes it.
QUICK_TOOLS = ["Read", "Grep", "Glob"]

# Lines shown either side of the passage. Enough for the surrounding argument
# and the notation in play; anything further is a Read away.
CONTEXT_LINES = 30

# The quick session keeps its conversation, which helps (the paper is in the
# cache) until it grows; past this many requests it starts afresh.
MAX_REQUESTS = 20

# Where texai's speed tests found Claude no slower than at any other level and
# never thinking on an ordinary edit.
DEFAULT_EFFORT = "medium"

SYSTEM_PROMPT = """
You edit one passage of a LaTeX document for texai. The user marked the passage
in the compiled PDF and said what they want. You get the passage exactly as it
is in the file, its line numbers, and the lines around it.

Answer with exactly one of these and nothing else:

<edit>
the complete new text of the passage, from its first line to its last
</edit>
<note>one short line saying what you changed</note>

<versions>
<version label="a few words on how it differs">the complete new text of the passage</version>
(2 to 4 versions that are genuinely different)
</versions>

<reply>your answer, when the user asked a question rather than for a change</reply>

<agent>why</agent>
(when the request needs more than rewriting this passage: changes elsewhere or
in other files, new tables or figures, anything that replacing these lines
cannot do)

Rules:
- Your text replaces the passage's lines as a whole, so it must contain
  everything that should stay: comment lines, `%@` annotations, labels and
  citations, unless the request is about them. A ` %#id` at the end of a line
  marks what is attached to that sentence: keep it at the end of the same
  sentence, exactly as written.
- Keep the document's conventions: its macros, notation and citation style, and
  how it wraps lines (similar line lengths, breaks where the original has them).
- Make the change the request asks for, in full, and nothing beyond it. Asked
  to shorten or tighten, come back noticeably shorter; never hand the passage
  back nearly as it was. The note says what changed, plainly.
- Offer versions when the request has several good answers ("tighten this", "a
  better title"). When it says how many versions are wanted, follow that; when
  it asks for a single edit, give an edit.
- The lines shown are usually enough. If you need something else from the
  document (a definition, the notation, what a reference points to), use Read
  or Grep, then answer. Do not narrate.
""".strip()


class QuickStale(RuntimeError):
    """The passage is no longer in the file as it was shown to the model."""


@dataclass(frozen=True)
class Passage:
    """The paragraph a request points at, and the lines around it."""

    file: str
    start: int
    end: int
    text: str
    context: str


@dataclass
class QuickAnswer:
    kind: str  # edit | versions | reply | agent
    text: str = ""
    note: str = ""
    versions: list[dict[str, str]] = field(default_factory=list)


def quick_eligible(message: str, selections: list[SelectionRef]) -> bool:
    """A request about one marked passage, and nothing else: what the page's
    ask box sends. Composer messages, with their free text, go to the agent."""
    return (
        not message.strip()
        and len(selections) == 1
        and bool((selections[0].instruction or "").strip())
    )


def find_passage(config: AppConfig, selection: SelectionRef) -> Passage | None:
    """The paragraph holding the selected line, or None when there is none."""
    try:
        source = read_source(config, selection.file)
    except SourceError:
        return None
    text = source["text"]
    lines = text.split("\n")
    for _index, first, last in paragraph_ranges(text):
        if first <= selection.line <= last:
            break
    else:
        return None

    low = max(1, first - CONTEXT_LINES)
    high = min(len(lines), last + CONTEXT_LINES)
    width = len(str(high))
    context = "\n".join(
        f"{number:>{width}}{'›' if first <= number <= last else '|'} {lines[number - 1]}"
        for number in range(low, high + 1)
    )
    return Passage(
        file=source["file"],
        start=first,
        end=last,
        text="\n".join(lines[first - 1 : last]),
        context=context,
    )


def compose_quick_prompt(passage: Passage, selection: SelectionRef) -> str:
    parts = [
        f"File: {passage.file}",
        f"Passage: lines {passage.start}-{passage.end} (marked › below)",
        f"<context>\n{passage.context}\n</context>",
        f"The passage, exactly as it is in the file:\n<passage>\n{passage.text}\n</passage>",
    ]
    if selection.selectedText:
        parts.append(f'The user selected this rendered text in it: "{selection.selectedText}"')
    elif selection.word:
        parts.append(f'The user pointed at the word: "{selection.word}"')
    parts.append(f"Request: {(selection.instruction or '').strip()}")
    if selection.alternatives == 1:
        parts.append("Wanted: a single edit.")
    elif selection.alternatives:
        parts.append(f"Wanted: {selection.alternatives} versions to choose from.")
    return "\n\n".join(parts)


def _tag(text: str, name: str) -> str | None:
    match = re.search(rf"<{name}>\n?(.*?)\n?</{name}>", text, re.DOTALL)
    return match.group(1) if match else None


def parse_answer(text: str) -> QuickAnswer:
    """The answer's one action; ``agent`` when there is none texai can use."""
    edit = _tag(text, "edit")
    if edit is not None:
        return QuickAnswer("edit", text=edit, note=(_tag(text, "note") or "").strip())
    versions = _tag(text, "versions")
    if versions is not None:
        found = re.findall(r'<version\s+label="([^"]*)">\n?(.*?)\n?</version>', versions, re.DOTALL)
        return QuickAnswer("versions", versions=[{"label": l, "text": t} for l, t in found])
    reply = _tag(text, "reply")
    if reply is not None:
        return QuickAnswer("reply", text=reply.strip())
    why = _tag(text, "agent")
    if why is not None:
        return QuickAnswer("agent", text=why.strip())
    return QuickAnswer("agent", text="the quick editor's answer was not one texai could apply")


def apply_edit(config: AppConfig, passage: Passage, new_text: str) -> None:
    """Put ``new_text`` in place of the passage's lines.

    Raises :class:`QuickStale` when those lines no longer hold the passage —
    edited meanwhile, by hand or by another turn.
    """
    try:
        source = read_source(config, passage.file)
    except SourceError as exc:
        raise QuickStale(str(exc)) from exc
    lines = source["text"].split("\n")
    if "\n".join(lines[passage.start - 1 : passage.end]) != passage.text:
        raise QuickStale(f"{passage.file}:{passage.start} has changed since it was read")
    lines[passage.start - 1 : passage.end] = new_text.split("\n")
    atomic_write_text(config.root / source["file"], "\n".join(lines))


class QuickSession(AgentSession):
    """The quick editor's own Claude session: read-only, hermetic, terse."""

    name = "Claude (quick)"
    primary = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.effort = self.effort or DEFAULT_EFFORT
        self._requests = 0

    def _options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            cwd=str(self.config.root),
            tools=list(QUICK_TOOLS),
            allowed_tools=list(QUICK_TOOLS),
            strict_mcp_config=True,
            include_partial_messages=True,
            # Hermetic: no CLAUDE.md, hooks or settings riding along on a call
            # whose whole point is to be short.
            setting_sources=[],
            system_prompt=SYSTEM_PROMPT,
            effort=self.effort,
            # Not a conversation anyone returns to: kept out of the sessions
            # the panel offers to resume.
            extra_args={"no-session-persistence": None},
            **({"model": self.model} if self.model else {}),
        )

    async def run_turn(self, prompt: str, turn_id: str) -> Any:
        if self._requests >= MAX_REQUESTS:
            await self.stop()
            self._requests = 0
        if not self.running:
            await self.start()
        self._requests += 1
        return await super().run_turn(prompt, turn_id)
