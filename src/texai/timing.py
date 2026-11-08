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

When the agent also passes on the raw API stream (Claude does), each model
request is timed from the inside as well: how long it waited for the first
token, how long each block took to generate, and how many tokens went in and
came out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .selection import SELECTION_DIRNAME

__all__ = [
    "TIMINGS_FILENAME",
    "AgentClock",
    "Stopwatch",
    "estimate_turn",
    "record",
    "timings_path",
    "seconds",
]

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


# What a turn takes before there is any history to go on: texai's own speed
# tests on a short paper (sentence and paragraph edits, versions, Opus 5).
DEFAULT_WORK_MS = {
    ("quick", False): 3_000,
    ("quick", True): 4_500,
    ("agent", False): 8_000,
    ("agent", True): 14_000,
    ("pick", False): 0,
}
DEFAULT_BUILD_MS = 2_000
DEFAULT_FIX_MS = 8_000
# Recent turns are what predicts the next one: the same paper, model and machine.
RECENT = 15
SCANNED_LINES = 400
_WORK_PHASE = {"quick": "quickMs", "agent": "agentMs", "pick": "pickMs"}


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _recent_turns(root: Path) -> list[dict[str, Any]]:
    try:
        lines = timings_path(root).read_text(encoding="utf-8").splitlines()[-SCANNED_LINES:]
    except OSError:
        return []
    turns = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind") == "turn":
            turns.append(entry)
    return turns


def estimate_turn(root: Path, route: str, versions: bool = False) -> dict[str, Any]:
    """How long a turn on ``route`` should take, phase by phase, for a progress bar.

    The median of the recent turns that went the same way (quick or agent,
    asking for versions or not) for the model's part, and of recent first
    builds for the compile; texai's measured defaults until there are some.
    """
    history = _recent_turns(root)
    phase = _WORK_PHASE.get(route, "agentMs")
    same = [
        t for t in history
        if t.get("route") == route
        and bool(t.get("versions")) == versions
        and t.get("status") in ("applied", "answered")
        and isinstance((t.get("phases") or {}).get(phase), int)
    ][-RECENT:]
    builds = [t["builds"][0]["ms"] for t in history if t.get("builds") and t["builds"][0].get("ok")]
    fixes = [t["phases"]["fixMs"] for t in history if isinstance((t.get("phases") or {}).get("fixMs"), int)]

    work = _median([t["phases"][phase] for t in same])
    if work is None:
        work = DEFAULT_WORK_MS.get((route, versions), DEFAULT_WORK_MS.get((route, False), 8_000))
    build = _median(builds[-RECENT:]) or DEFAULT_BUILD_MS
    phases = [{"name": "compiling", "ms": build}]
    if route != "pick":
        phases.insert(0, {"name": "writing", "ms": work})
    return {
        "phases": phases,
        "fixMs": _median(fixes[-RECENT:]) or DEFAULT_FIX_MS,
        "samples": len(same),
    }


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
        # Per model request, from the raw stream: see stream().
        self.requests: list[dict[str, Any]] = []
        self._ready = self._watch.started  # when the next request could start
        self._request: dict[str, Any] | None = None
        self._block: dict[str, Any] | None = None

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
        self._ready = time.monotonic()

    def stream(self, event: dict[str, Any]) -> None:
        """One raw API stream event, timing the request it belongs to.

        ``waitMs`` runs from when the request could start (the turn being sent,
        or the last tool result coming back) to the API's first reply, which
        arrives with the first token: the agent's own overhead, the network,
        any queue, and reading the prompt. ``generateMs`` is the rest.
        """
        now = time.monotonic()
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            self._request = {
                "id": message.get("id"),
                "waitMs": _ms(now - self._ready),
                "inputTokens": usage.get("input_tokens"),
                "cacheReadTokens": usage.get("cache_read_input_tokens"),
                "cacheWriteTokens": usage.get("cache_creation_input_tokens"),
                "blocks": [],
                "_started": now,
            }
            self.requests.append(self._request)
            return
        request = self._request
        if request is None:
            return
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            self._block = {"type": block.get("type"), "_started": now}
        elif kind == "content_block_stop" and self._block is not None:
            started = self._block.pop("_started")
            request["blocks"].append({**self._block, "ms": _ms(now - started)})
            self._block = None
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            if "output_tokens" in usage:
                request["outputTokens"] = usage["output_tokens"]
        elif kind == "message_stop":
            request["generateMs"] = _ms(now - request.pop("_started"))
            if request.get("outputTokens") and request["generateMs"]:
                request["tokensPerS"] = round(
                    request["outputTokens"] / (request["generateMs"] / 1000), 1
                )
            self._request = None
            self._ready = now

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
            **_request_totals(self.requests),
            **self.extra,
            "requests": self.requests,
            "spans": self.spans,
        }


def _ms(seconds_: float) -> int:
    return round(seconds_ * 1000)


def _request_totals(requests: list[dict[str, Any]]) -> dict[str, Any]:
    """The per-request stream timings, added up; nothing when there were none."""
    if not requests:
        return {}
    return {
        "waitMs": sum(r.get("waitMs", 0) for r in requests),
        "generateMs": sum(r.get("generateMs", 0) for r in requests),
        "outputTokens": sum(r.get("outputTokens") or 0 for r in requests),
    }
