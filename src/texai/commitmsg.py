"""Ask the agent to write a commit message for what is uncommitted.

This is deliberately a one-shot query rather than a turn on the review session:
it must not appear in the transcript, must not touch the review baseline, and
must not be able to edit anything. It gets a diff and returns prose — no tools,
one turn, and a plain summary as the fallback when the agent is unavailable.
"""

from __future__ import annotations

import asyncio

from .agent import sdk_status
from .config import AppConfig
from .git import GitStatus, summarize

__all__ = ["propose_message", "MESSAGE_TIMEOUT"]

MESSAGE_TIMEOUT = 90
MAX_SUBJECT = 72

SYSTEM_PROMPT = f"""You write git commit messages for a LaTeX writing project.

Given a diff, reply with the commit message and nothing else — no preamble, no
explanation, no code fences, no quotes around it.

Rules:
- First line: imperative mood, under {MAX_SUBJECT} characters, no trailing period.
- Then, only if the change genuinely needs it, a blank line and one or two short
  lines of body. Most changes do not need a body.
- Describe what changed in the writing, not the mechanics of the diff.
  "Tighten the identification argument" beats "edit model.tex".
- Never mention Claude, AI, or this tool.
"""


async def propose_message(
    config: AppConfig,
    status: GitStatus,
    diff: str,
    model: str | None = None,
) -> dict[str, str]:
    """Return ``{"message": ..., "source": "agent"|"fallback", "reason": ...}``.

    Never raises: a commit message is not worth failing a request over, so any
    problem degrades to the plain summary with the reason attached.
    """
    fallback = {"message": summarize(status), "source": "fallback"}

    if not diff.strip():
        return {**fallback, "reason": "Nothing is uncommitted."}

    available, why = sdk_status()
    if not available:
        return {**fallback, "reason": why or "The agent is unavailable."}

    try:
        text = await asyncio.wait_for(_ask(config, diff, model), timeout=MESSAGE_TIMEOUT)
    except asyncio.TimeoutError:
        return {**fallback, "reason": f"The agent took longer than {MESSAGE_TIMEOUT}s."}
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the request
        return {**fallback, "reason": f"{type(exc).__name__}: {exc}"}

    cleaned = _clean(text)
    if not cleaned:
        return {**fallback, "reason": "The agent returned nothing usable."}
    return {"message": cleaned, "source": "agent"}


async def _ask(config: AppConfig, diff: str, model: str | None) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        cwd=str(config.root),
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        # No tools at all: this call reads a diff and writes a sentence.
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"],
        max_turns=1,
        setting_sources=[],  # hermetic: no project settings, no CLAUDE.md
        **({"model": model} if model else {}),
    )

    prompt = (
        "Write a commit message for these uncommitted changes.\n\n"
        f"```diff\n{diff}\n```"
    )

    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts)


def _clean(text: str) -> str:
    """Strip the wrappers models reach for even when told not to."""
    body = text.strip()

    if body.startswith("```"):
        lines = body.splitlines()
        lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        body = "\n".join(lines).strip()

    # A whole message wrapped in quotes, which happens when the subject is short.
    if len(body) > 1 and body[0] == body[-1] and body[0] in {'"', "'"} and "\n" not in body:
        body = body[1:-1].strip()

    lines = [line.rstrip() for line in body.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    lines[0] = lines[0].lstrip("#- ").rstrip(".").strip()
    return "\n".join(lines).strip()
