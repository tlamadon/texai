"""Transcript entries: one record of what the agent did, rendered two ways.

The chat view shows a friendly summary of these; the transcript view shows them
verbatim in the shape a terminal session would. Building both from a single
stream keeps the two views honestly in sync — the transcript cannot show
something the chat silently dropped.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_DETAIL_CHARS",
    "flatten_tool_result",
    "summarize_result",
    "text_entry",
    "thinking_entry",
    "tool_use_entry",
    "tool_result_entry",
    "system_entry",
    "build_entry",
    "result_entry",
    "notice_entry",
    "user_entry",
]

MAX_DETAIL_CHARS = 4000
MAX_SUMMARY_CHARS = 160


def _entry(kind: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": kind}
    entry.update({k: v for k, v in fields.items() if v is not None})
    return entry


def flatten_tool_result(content: Any) -> str:
    """Tool results arrive as a string or a list of content blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def summarize_result(text: str) -> str:
    """A one-line gist of a tool result, in the style of a terminal log."""
    stripped = text.strip()
    if not stripped:
        return "(no output)"
    lines = stripped.splitlines()
    head = lines[0].strip()
    if len(head) > MAX_SUMMARY_CHARS:
        head = f"{head[: MAX_SUMMARY_CHARS - 1]}…"
    if len(lines) > 1:
        return f"{head} (+{len(lines) - 1} more lines)"
    return head


def user_entry(text: str) -> dict[str, Any]:
    return _entry("user", text=text)


def text_entry(text: str) -> dict[str, Any]:
    return _entry("text", text=text)


def thinking_entry() -> dict[str, Any]:
    # The content of thinking blocks is not surfaced; this marks that it happened.
    return _entry("thinking", text="thinking…")


def tool_use_entry(name: str, summary: str, tool_input: dict[str, Any] | None = None) -> dict[str, Any]:
    detail = None
    if tool_input:
        pairs = [f"{key}: {str(value)[:400]}" for key, value in tool_input.items()]
        detail = "\n".join(pairs)[:MAX_DETAIL_CHARS]
    return _entry("tool_use", verb=name, text=summary, detail=detail)


def tool_result_entry(name: str, content: Any, is_error: bool = False) -> dict[str, Any]:
    """A tool result line.

    Full output is kept only for errors. A successful ``Read`` would otherwise
    dump the whole file into the transcript, which is exactly the noise a
    terminal session does *not* show you.
    """
    text = flatten_tool_result(content)
    return _entry(
        "tool_result",
        verb=name,
        text=summarize_result(text),
        detail=(text[:MAX_DETAIL_CHARS] or None) if is_error else None,
        isError=True if is_error else None,
    )


# The SDK emits a stream of session bookkeeping (init handshakes, token counts,
# thinking-token tallies) that no one wants in a transcript. Allow-list rather
# than deny-list: new bookkeeping subtypes appear over time, and a transcript
# should stay readable by default rather than after each new one is discovered.
NOTABLE_SYSTEM_MARKERS = ("compact", "error", "warn", "refus", "limit", "interrupt")


def system_entry(subtype: str, data: Any = None) -> dict[str, Any] | None:
    """A system line, or ``None`` when it is routine bookkeeping."""
    name = (subtype or "").strip()
    if not name or not any(marker in name.lower() for marker in NOTABLE_SYSTEM_MARKERS):
        return None

    text = ""
    if isinstance(data, dict):
        for key in ("message", "reason", "error", "summary"):
            if data.get(key):
                text = str(data[key])[:MAX_SUMMARY_CHARS]
                break
    return _entry("system", verb=name, text=text or name)


def build_entry(text: str, is_error: bool = False, detail: str | None = None) -> dict[str, Any]:
    return _entry(
        "build",
        verb="build",
        text=text,
        detail=(detail or None),
        isError=True if is_error else None,
    )


def notice_entry(text: str, is_error: bool = False) -> dict[str, Any]:
    return _entry("notice", text=text, isError=True if is_error else None)


def result_entry(
    *,
    status: str,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    num_turns: int | None = None,
) -> dict[str, Any]:
    bits = [status]
    if num_turns:
        bits.append(f"{num_turns} turn{'s' if num_turns != 1 else ''}")
    if duration_ms:
        bits.append(f"{duration_ms / 1000:.1f}s")
    if cost_usd:
        bits.append(f"${cost_usd:.4f}")
    return _entry(
        "result",
        text=" · ".join(bits),
        costUsd=cost_usd,
        durationMs=duration_ms,
        numTurns=num_turns,
    )
