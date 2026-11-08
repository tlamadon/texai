"""Where the time goes: a turn split into its phases, written down.

Speeding the loop up starts with knowing what is slow, and a turn has several
suspects — the agent session starting, the model thinking, the model writing,
tools running, the compile, a fix-up round after a broken build. Each turn (and
each commit message and editor completion) leaves one JSON line in
``.texai/timings.jsonl`` saying how long each of those took, with the model and
effort level it ran at, so changes can be compared on numbers rather than feel.

Inside an agent call there are no timestamps to read, only messages arriving.
So the time between two arrivals is charged to whatever the second one is: a
thinking block means the model was thinking, a tool call that it was writing
one, a tool result that the tool was running. The first block of each model
call also carries the wait before the model started answering, and is marked
so that wait can be told apart.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .selection import SELECTION_DIRNAME

__all__ = ["TIMINGS_FILENAME", "AgentClock", "Stopwatch", "record", "timings_path", "seconds"]

TIMINGS_FILENAME = "timings.jsonl"

# A model request whose start has been seen but whose id has not, yet.
_PENDING: Any = object()


def timings_path(root: Path) -> Path:
    return root / SELECTION_DIRNAME / TIMINGS_FILENAME


def record(root: Path, entry: dict[str, Any]) -> None:
    """Append one timing record. Best effort: a full disk must not fail a turn."""
    path = timings_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def seconds(ms: int | float | None) -> str:
    return f"{(ms or 0) / 1000:.1f}s"


class Stopwatch:
    """Milliseconds since it was made, or since the last lap."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self._last = self.started

    def lap(self) -> int:
        now = time.monotonic()
        elapsed = round((now - self._last) * 1000)
        self._last = now
        return elapsed

    def total(self) -> int:
        return round((time.monotonic() - self.started) * 1000)


class AgentClock:
    """Splits one agent call's wall time between the model, the tools and the harness."""

    def __init__(self) -> None:
        self._watch = Stopwatch()
        self.spans: list[dict[str, Any]] = []
        self.models: list[str] = []
        self.calls = 0
        self._call: Any = None
        self.extra: dict[str, Any] = {}

    def model(self, block: str, call: str | None = None, model: str | None = None) -> None:
        """A model block arrived: ``thinking``, ``text`` or ``tool_use``.

        ``call`` identifies the model request it belongs to; a new one means a
        new request, whose first block also carries the wait for it to start.
        Without one, a block that follows anything but another model block
        starts a new request.
        """
        if self._call is _PENDING:
            first = False  # its start was already counted by thinking()
        elif call is not None:
            first = call != self._call
        else:
            first = not self.spans or self.spans[-1]["what"] != "model"
        self._call = call
        if first:
            self.calls += 1
        if model and model not in self.models:
            self.models.append(model)
        span: dict[str, Any] = {"what": "model", "block": block, "ms": self._watch.lap()}
        if first:
            span["first"] = True
        self.spans.append(span)

    def thinking(self) -> None:
        """The model reported thinking progress before any block of its answer.

        Claude Code sends these while a request is still thinking, without
        saying which request; the block that follows will.
        """
        first = not self.spans or self.spans[-1]["what"] != "model"
        if first:
            self.calls += 1
            self._call = _PENDING
        span: dict[str, Any] = {"what": "model", "block": "thinking", "ms": self._watch.lap()}
        if first:
            span["first"] = True
        self.spans.append(span)

    def tool(self, name: str) -> None:
        """A tool result arrived: the time since was the tool running."""
        self.spans.append({"what": "tool", "tool": name, "ms": self._watch.lap()})

    def harness(self, what: str) -> None:
        """Anything else: the agent's own bookkeeping, start-up, the final result."""
        self.spans.append({"what": "harness", "event": what, "ms": self._watch.lap()})

    def summary(self) -> dict[str, Any]:
        model_ms = sum(s["ms"] for s in self.spans if s["what"] == "model")
        tools: dict[str, int] = {}
        for span in self.spans:
            if span["what"] == "tool":
                tools[span["tool"]] = tools.get(span["tool"], 0) + span["ms"]
        return {
            "wallMs": self._watch.total(),
            "modelMs": model_ms,
            "thinkingMs": sum(
                s["ms"] for s in self.spans if s["what"] == "model" and s["block"] == "thinking"
            ),
            "firstBlockMs": sum(s["ms"] for s in self.spans if s.get("first")),
            "toolMs": sum(tools.values()),
            "harnessMs": sum(s["ms"] for s in self.spans if s["what"] == "harness"),
            "tools": tools,
            "modelCalls": self.calls,
            "models": self.models,
            **self.extra,
            "spans": self.spans,
        }
