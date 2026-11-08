"""The editing agent, driven through OpenAI's Codex SDK instead of Claude's.

Same contract as :class:`texai.agent.AgentSession` — one long-lived thread per
project, every event republished on the bus as a transcript entry, a
:class:`TurnOutcome` per turn — so the turn controller, the snapshots and the
revert treat the two alike.

Two things differ, both because Codex is its own process with its own model of
tools:

* **How it edits.** Codex has no files-only mode: it reads with shell commands
  and writes with patches. The thread runs in Codex's ``workspace-write``
  sandbox, which confines writes to the project and keeps the network off, and
  with approvals denied, so nothing ever waits on a prompt nobody will see. That
  is a looser promise than Claude's "file tools and nothing else", and the
  README says so.
* **How it reaches texai's tools.** Claude takes ``show_in_pdf`` in-process;
  Codex is pointed at the MCP endpoint texai serves on its own loopback port
  (see :mod:`texai.agent_tools`), with that one tool pre-approved.

The SDK is an optional extra (``texai[codex]``): it carries a few hundred MB of
Codex runtime that a Claude user should not have to download.
"""

from __future__ import annotations

import asyncio
import shlex
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from . import transcript
from .agent import AgentUnavailable, TurnOutcome, _relative
from .config import AppConfig
from .events import EventBus
from .prompt import SYSTEM_PROMPT_APPEND
from .timing import AgentClock

__all__ = ["CodexSession", "CodexTranscript", "codex_status", "codex_one_shot"]

INSTALL_HINT = (
    "The Codex SDK is not installed. It is an optional extra: run "
    "`uvx 'texai[codex]' ...` or `pip install 'texai[codex]'`, and sign in with "
    "`codex login` if you have not already."
)

# What Codex needs to hear on top of the shared instructions. The shared ones
# were written for an agent without a shell; this one has one.
CODEX_INSTRUCTIONS = (
    SYSTEM_PROMPT_APPEND
    + """

You are running under Codex. Edit files with apply_patch. Shell commands are
fine for finding your way around (rg, sed -n, cat), but never run latexmk,
pdflatex, or anything else that compiles or writes build output: texai owns the
build and runs it when you finish. The `show_in_pdf` and `offer_options` tools
live on the `texai` MCP server.

Your replies are shown as plain text, not rendered markdown. Refer to places as
`file:line` relative to the project root (sections/model.tex:12), never as a
markdown link or an absolute path."""
)


_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})


def codex_status() -> tuple[bool, str | None]:
    """Whether Codex can run here, and why not if it cannot.

    Only the SDK is checked: it brings its own Codex runtime, and a missing
    login surfaces as a plain error on the first turn, in the SDK's own words.
    """
    try:
        import openai_codex  # noqa: F401
    except ImportError:
        return False, INSTALL_HINT
    return True, None


def _dump(payload: Any) -> dict[str, Any]:
    """A notification payload as plain JSON data, whatever the SDK typed it as."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json", exclude_none=True)
    params = getattr(payload, "params", None)  # UnknownNotification
    return params if isinstance(params, dict) else {}


def _command_text(item: dict[str, Any]) -> str:
    """The command as typed, not wrapped in the shell Codex launches it with."""
    actions = item.get("command_actions") or []
    if len(actions) == 1 and actions[0].get("command"):
        return str(actions[0]["command"])
    command = str(item.get("command", ""))
    # `/path/to/zsh -lc "…"`: show the script, not the launcher.
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if len(argv) == 3 and Path(argv[0]).name in _SHELLS and argv[1] in ("-c", "-lc"):
        return argv[2]
    return command


class CodexTranscript:
    """Turns Codex's item events into the transcript entries the chat renders.

    Codex reports each piece of work as an item that starts and later completes:
    a message, a burst of reasoning, a shell command, a patch, an MCP tool call.
    A tool line goes out when the item starts, so the panel shows what is
    running; its result when it completes. Kept apart from the session, and fed
    plain dicts, so it can be tested without the SDK.
    """

    def __init__(self, root: Any) -> None:
        self.root = root
        self.texts: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self._started: set[str] = set()

    def _use(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        kind = item.get("type")
        if kind == "commandExecution":
            verb, summary, detail = "Shell", _command_text(item), None
        elif kind == "fileChange":
            paths = [_relative(c.get("path"), self.root) for c in item.get("changes") or []]
            verb, summary, detail = "Edit", ", ".join(p for p in paths if p), None
        elif kind == "mcpToolCall":
            verb = str(item.get("tool") or "tool")
            arguments = item.get("arguments")
            detail = arguments if isinstance(arguments, dict) else None
            summary = ""
            if detail and detail.get("file"):
                summary = f"{detail['file']}:{detail.get('line', '')}".rstrip(":")
        elif kind == "webSearch":
            verb, summary, detail = "WebSearch", str(item.get("query") or ""), None
        else:
            return []
        if item.get("id"):
            self._started.add(item["id"])
        self.tool_calls.append({"name": verb, "summary": summary})
        return [transcript.tool_use_entry(verb, summary, detail)]

    def _result(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        kind = item.get("type")
        status = item.get("status")
        failed = status in ("failed", "declined")
        if kind == "commandExecution":
            output = item.get("aggregated_output") or ""
            # Not a failure in itself: rg exits 1 when nothing matches.
            code = item.get("exit_code")
            if code not in (None, 0):
                output = f"exit {code}: {output}"
            return [transcript.tool_result_entry("Shell", output, failed)]
        if kind == "fileChange":
            changes = item.get("changes") or []
            text = "declined" if status == "declined" else f"{len(changes)} file(s) changed"
            return [transcript.tool_result_entry("Edit", text, failed)]
        if kind == "mcpToolCall":
            error = (item.get("error") or {}).get("message")
            content = error or (item.get("result") or {}).get("content") or ""
            return [
                transcript.tool_result_entry(str(item.get("tool") or "tool"), content, failed)
            ]
        return []

    def feed(self, method: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        """The entries one notification produces; most produce none."""
        item = data.get("item") if isinstance(data.get("item"), dict) else None
        if method == "item/started" and item:
            return self._use(item)
        if method == "item/completed" and item:
            kind = item.get("type")
            if kind == "agentMessage":
                text = str(item.get("text") or "")
                if not text.strip():
                    return []
                self.texts.append(text)
                return [transcript.text_entry(text)]
            if kind == "reasoning":
                return [transcript.thinking_entry()]
            # A tool that finished without our seeing it start still gets its line.
            head = [] if item.get("id") in self._started else self._use(item)
            return head + self._result(item)
        if method == "error":
            error = data.get("error") or {}
            if not data.get("will_retry") and error.get("message"):
                return [transcript.notice_entry(str(error["message"]), is_error=True)]
        return []


# Codex items that are tools at work, and the verb the transcript gives them.
_TOOL_VERBS = {"commandExecution": "Shell", "fileChange": "Edit", "webSearch": "WebSearch"}


def tick(clock: AgentClock, method: str, data: dict[str, Any]) -> None:
    """Charge the time since the last notification to what this one reports.

    A tool item starting means the model was writing the call; one completing,
    that the tool was running. Reasoning and messages completing are the model.
    """
    item = data.get("item") if isinstance(data.get("item"), dict) else None
    kind = item.get("type") if item else None
    is_tool = kind in _TOOL_VERBS or kind == "mcpToolCall"
    if method == "item/started" and is_tool:
        clock.model("tool_use")
    elif method == "item/completed" and is_tool:
        clock.tool(_TOOL_VERBS.get(kind) or str(item.get("tool") or "tool"))
    elif method == "item/completed" and kind == "reasoning":
        clock.model("thinking")
    elif method == "item/completed" and kind == "agentMessage":
        clock.model("text")
    elif method == "turn/completed":
        clock.harness("result")


def _thread_config(tools_url: str | None) -> dict[str, Any] | None:
    """Point the thread at texai's MCP endpoint, with its tools pre-approved.

    Approvals are denied for the thread as a whole, and an MCP tool counts as
    something to approve, so without the per-tool override every call to
    ``show_in_pdf`` or ``offer_options`` would be refused.
    """
    if not tools_url:
        return None
    return {
        "mcp_servers": {
            "texai": {
                "url": tools_url,
                "required": True,
                "tools": {
                    "show_in_pdf": {"approval_mode": "approve"},
                    "offer_options": {"approval_mode": "approve"},
                },
            }
        }
    }


class CodexSession:
    """A single persistent Codex thread scoped to one project."""

    kind = "codex"
    name = "Codex"

    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        model: str | None = None,
        tools_url: str | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.model = model
        self.effort: str | None = None  # Codex's reasoning effort is its own config
        self.tools_url = tools_url
        self._stack: AsyncExitStack | None = None
        self._thread: Any = None
        self._turn: Any = None
        self._lock = asyncio.Lock()
        self._started = False
        self._session_id: str | None = None

    @property
    def running(self) -> bool:
        return self._started

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def resume_command(self) -> str | None:
        return f"codex resume {self._session_id}" if self._session_id else None

    def status(self) -> tuple[bool, str | None]:
        return codex_status()

    async def start(self) -> None:
        """Start the thread. Raises :class:`AgentUnavailable` if it cannot."""
        if self._started:
            return
        ok, reason = self.status()
        if not ok:
            raise AgentUnavailable(reason or "agent unavailable")

        from openai_codex import ApprovalMode, AsyncCodex, Sandbox

        stack = AsyncExitStack()
        try:
            codex = await stack.enter_async_context(AsyncCodex())
            self._thread = await codex.thread_start(
                cwd=str(self.config.root),
                sandbox=Sandbox.workspace_write,
                approval_mode=ApprovalMode.deny_all,
                developer_instructions=CODEX_INSTRUCTIONS,
                config=_thread_config(self.tools_url),
                **({"model": self.model} if self.model else {}),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            await stack.aclose()
            raise AgentUnavailable(f"Could not start the Codex session: {exc}") from exc

        self._stack = stack
        self._started = True
        # Codex names the thread up front, so the terminal can attach before
        # the first turn rather than after it.
        self._session_id = self._thread.id
        self.bus.publish(
            "agent_session", sessionId=self._session_id, resumeCommand=self.resume_command()
        )
        self.bus.publish("agent_ready", model=self.model)

    async def run_turn(self, prompt: str, turn_id: str) -> TurnOutcome:
        """Send one turn and stream everything it produces onto the bus."""
        if not self._started or self._thread is None:
            raise AgentUnavailable("agent session is not running")

        log = CodexTranscript(self.config.root)
        entries: list[dict[str, Any]] = []
        status: str | None = None
        error: str | None = None
        duration_ms: int | None = None
        clock = AgentClock()

        def emit(entry: dict[str, Any]) -> None:
            entries.append(entry)
            self.bus.publish("agent_entry", turnId=turn_id, entry=entry)

        async with self._lock:
            try:
                self._turn = await self._thread.turn(prompt)
                async for notification in self._turn.stream():
                    data = _dump(notification.payload)
                    tick(clock, notification.method, data)
                    for entry in log.feed(notification.method, data):
                        emit(entry)
                    if notification.method == "turn/completed":
                        turn = data.get("turn") or {}
                        status = turn.get("status")
                        duration_ms = turn.get("duration_ms")
                        if status == "failed":
                            error = (turn.get("error") or {}).get("message") or "the turn failed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
                error = str(exc)
            finally:
                self._turn = None

        if status is not None:
            emit(
                transcript.result_entry(
                    status="success" if status == "completed" else status,
                    duration_ms=duration_ms,
                )
            )
        if error:
            emit(transcript.notice_entry(error, is_error=True))
            self.bus.publish("agent_error", turnId=turn_id, message=error)
        return TurnOutcome(
            ok=error is None,
            text="\n\n".join(log.texts),
            tool_calls=log.tool_calls,
            entries=entries,
            error=error,
            terminal_reason=status,
            duration_ms=duration_ms,
            session_id=self._session_id,
            timing=clock.summary(),
        )

    async def interrupt(self) -> None:
        if self._started and self._turn is not None:
            await self._turn.interrupt()
            self.bus.publish("agent_interrupted")

    async def stop(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._stack = None
        self._thread = None
        self._started = False


async def codex_one_shot(config: AppConfig, system: str, prompt: str, model: str | None) -> str:
    """A single tool-less answer, for the commit message: read-only and ephemeral,
    so it neither edits anything nor lingers in the user's Codex history."""
    from openai_codex import ApprovalMode, AsyncCodex, Sandbox

    async with AsyncCodex() as codex:
        thread = await codex.thread_start(
            cwd=str(config.root),
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            developer_instructions=system,
            ephemeral=True,
            **({"model": model} if model else {}),
        )
        result = await thread.run(prompt)
    return result.final_response or ""
