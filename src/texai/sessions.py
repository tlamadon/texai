"""Which agent conversation the panel is talking to, and which others it could be.

texai keeps one agent session per launch, but the conversation itself outlives
the launch: Claude Code and Codex both save it, and either can pick it up again
by id. So the panel can resume where the last launch left off, switch between
conversations, or take over one started in a terminal.

The agent's own store says which conversations exist for this directory. What
it cannot say is which of them texai ran, how many review turns each took, or
what the user actually asked first (the store keeps the composed prompt, with
the marked passages folded in). That much is written to
``.texai/sessions.json`` as turns finish, and merged into the store's list.

Turns that ran outside texai have no snapshots, so a conversation taken over
from a terminal can be read and continued, but not reverted: the review starts
from the files as they stand.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .selection import SELECTION_DIRNAME

__all__ = [
    "SESSIONS_FILENAME",
    "catalogue",
    "conversation",
    "last_session",
    "load",
    "remember",
    "sessions_path",
    "shown_prompt",
]

SESSIONS_FILENAME = "sessions.json"

# A first message is a title, not a transcript.
MAX_TITLE = 120
# Enough to cover a long project's history without making the list a scroll.
MAX_REMEMBERED = 100


def sessions_path(root: Path) -> Path:
    return root / SELECTION_DIRNAME / SESSIONS_FILENAME


def _now_ms() -> int:
    return int(time.time() * 1000)


def _title(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1] + "…"


def load(root: Path) -> list[dict[str, Any]]:
    """The sessions texai has run in this project, newest first."""
    try:
        data = json.loads(sessions_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    kept = [e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]
    return sorted(kept, key=lambda e: e.get("updatedAt") or 0, reverse=True)


def remember(
    root: Path,
    agent: str,
    session_id: str,
    *,
    title: str | None = None,
    turned: bool = False,
    imported: bool = False,
) -> dict[str, Any]:
    """Note that texai used this session; one more turn when ``turned``.

    The first title given sticks: it is what the conversation was started for.
    A conversation ``imported`` from elsewhere was started for something else,
    so it keeps the title its own store gives it.
    Best effort, like the timings file — a full disk must not fail a turn.
    """
    entries = load(root)
    entry = next((e for e in entries if e["id"] == session_id), None)
    now = _now_ms()
    if entry is None:
        entry = {"id": session_id, "agent": agent, "startedAt": now, "turns": 0}
        if imported:
            entry["imported"] = True
        entries.insert(0, entry)
    entry["updatedAt"] = now
    if turned:
        entry["turns"] = int(entry.get("turns") or 0) + 1
    if not entry.get("title") and not entry.get("imported") and _title(title):
        entry["title"] = _title(title)
    entries.sort(key=lambda e: e.get("updatedAt") or 0, reverse=True)
    path = sessions_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"sessions": entries[:MAX_REMEMBERED]}, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass
    return entry


def last_session(root: Path, agent: str) -> str | None:
    """The session texai used most recently here with this agent, if any."""
    return next((e["id"] for e in load(root) if e.get("agent") == agent), None)


def shown_prompt(text: str) -> str:
    """A past user turn as the user wrote it, not as texai composed it.

    texai folds the marked passages in ahead of the instruction (see
    texai.prompt); reading a conversation back, the instruction is the part
    worth showing, with a word on what it was about.
    """
    lines = text.strip().split("\n")
    # What texai told the agent it had missed (see TurnController._with_news).
    while lines and lines[0].startswith("(Since your last turn"):
        lines.pop(0)
    text = "\n".join(lines).strip()
    if not text.startswith("The user marked "):
        return text
    head = text.split(" while reading", 1)[0].removeprefix("The user marked ")
    _, found, instruction = text.partition("\n\nInstruction:\n")
    instruction = instruction.strip() if found else "Apply the request noted on each passage."
    return f"[{head} marked] {instruction}"


def conversation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plain ``{role, text}`` messages, with runs from one speaker joined.

    Agents split one reply over several records (thinking, then text, then a
    tool call); reading it back, that is one message.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        text = (message.get("text") or "").strip()
        if not text:
            continue
        role = message.get("role")
        if role == "user":
            text = shown_prompt(text)
        if out and out[-1]["role"] == role:
            out[-1]["text"] += "\n\n" + text
        else:
            out.append({"role": role, "text": text})
    return out


def catalogue(
    root: Path,
    agent: str,
    saved: list[dict[str, Any]] | None,
    current: str | None,
) -> list[dict[str, Any]]:
    """Every conversation the panel could switch to, newest first.

    ``saved`` is the agent's own list for this directory (id, title, createdAt,
    updatedAt, all times in ms) or None when it could not be read, in which case
    texai's own record stands in. A session texai remembers but the store no
    longer has cannot be resumed, so it is left out.
    """
    mine = {e["id"]: e for e in load(root) if e.get("agent") == agent}
    if saved is None:
        saved = [
            {"id": e["id"], "createdAt": e.get("startedAt"), "updatedAt": e.get("updatedAt")}
            for e in mine.values()
        ]
    sessions: list[dict[str, Any]] = []
    for item in saved:
        own = mine.get(item["id"])
        sessions.append(
            {
                "id": item["id"],
                "agent": agent,
                # A title the user gave it beats what texai remembers, which
                # beats the store's guess (often the composed prompt).
                "title": item.get("customTitle")
                or (own or {}).get("title")
                or _title(shown_prompt(item.get("title") or ""))
                or "(untitled)",
                "createdAt": item.get("createdAt") or (own or {}).get("startedAt"),
                "updatedAt": item.get("updatedAt") or (own or {}).get("updatedAt"),
                "texai": own is not None,
                "turns": (own or {}).get("turns"),
                "current": item["id"] == current,
            }
        )
    sessions.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
    return sessions
