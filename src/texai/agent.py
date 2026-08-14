"""The background Claude Agent SDK session that edits the document.

One long-lived session per project, so the agent accumulates knowledge of the
paper across turns instead of re-reading it every time. Everything it emits is
republished on the event bus, which is what the chat panel renders.

The SDK is an optional dependency: without it the viewer, SyncTeX bridge and
selection file all still work, and the chat panel reports why it is disabled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import transcript
from .config import AppConfig
from .events import EventBus
from .navigate import LocateError, locate
from .prompt import SYSTEM_PROMPT_APPEND

__all__ = ["AgentUnavailable", "TurnOutcome", "AgentSession", "sdk_status"]

# Everything the agent needs to edit LaTeX. Bash is deliberately absent: the
# harness owns compilation, so a shell would only add risk and thrash.
ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    # In-process tool that scrolls the user's PDF view (see build_navigation_server).
    "mcp__texai__show_in_pdf",
]
DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Task", "KillShell", "BashOutput"]

INSTALL_HINT = (
    "The Claude Agent SDK is not installed. Run `uv sync --group agent` "
    "(or `uv add claude-agent-sdk`) and restart texai."
)
CLI_HINT = (
    "The `claude` CLI was not found on PATH. The Agent SDK drives it, so install "
    "Claude Code and make sure `claude` is runnable."
)


class AgentUnavailable(RuntimeError):
    """The agent cannot run (SDK missing, CLI missing, or start-up failed)."""


@dataclass
class TurnOutcome:
    """What one agent turn produced."""

    ok: bool
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    terminal_reason: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    session_id: str | None = None


def sdk_status() -> tuple[bool, str | None]:
    """Whether the agent can run here, and why not if it cannot."""
    import shutil

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, INSTALL_HINT
    if shutil.which("claude") is None:
        return False, CLI_HINT
    return True, None


def _relative(path_value: Any, root: Path) -> str:
    """Show tool paths relative to the project root; they are long otherwise."""
    try:
        return str(Path(str(path_value)).resolve().relative_to(root))
    except (ValueError, OSError, TypeError):
        return str(path_value)


def summarize_tool_use(name: str, tool_input: dict[str, Any], root: Path) -> str:
    """A one-line description of a tool call for the activity log."""
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        return _relative(target, root) if target else ""
    if name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path")
        return f"{pattern} in {_relative(path, root)}" if path else str(pattern)
    for key in ("command", "description", "prompt"):
        if key in tool_input:
            return str(tool_input[key])[:200]
    return ""


SHOW_IN_PDF_DESCRIPTION = """
Scroll the user's PDF view to a source location, so they are looking at the
thing you are talking about.

Call this whenever the user asks to be shown, taken to, or pointed at something
in the document — a table, a figure, a definition, an equation, a section — and
whenever your answer refers to a specific place they would want to see. Find the
file and line first (Grep is usually enough), then call this with them.

It moves the view only; it changes nothing. Prefer calling it over describing
where something is in words.
""".strip()


def build_navigation_server(config: AppConfig, bus: EventBus) -> Any:
    """An in-process MCP server giving the agent one action: move the view.

    Runs in this process, so the handler can publish straight onto the event
    bus the browser is already listening to.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "show_in_pdf",
        SHOW_IN_PDF_DESCRIPTION,
        {
            "file": str,
            "line": int,
            "why": str,
        },
    )
    async def show_in_pdf(args: dict[str, Any]) -> dict[str, Any]:
        file = str(args.get("file", "")).strip()
        line = int(args.get("line", 1) or 1)
        why = str(args.get("why", "") or "").strip()

        try:
            found = await asyncio.to_thread(locate, config, file, line)
        except LocateError as exc:
            return {"content": [{"type": "text", "text": f"Could not show that: {exc}"}]}

        if not found["found"]:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"SyncTeX has no record of {found['file']}:{found['line']}, so the "
                            "view did not move. That line may produce no output (a comment, a "
                            "definition, or something inside an untracked environment) — try a "
                            "line that renders visible text."
                        ),
                    }
                ]
            }

        bus.publish("navigate", **found, why=why)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Showing {found['file']}:{found['line']} — the view moved to page "
                        f"{found['page']}."
                    ),
                }
            ]
        }

    return create_sdk_mcp_server(name="texai", version="1.0.0", tools=[show_in_pdf])


class AgentSession:
    """A single persistent agent conversation scoped to one project."""

    def __init__(self, config: AppConfig, bus: EventBus, model: str | None = None) -> None:
        self.config = config
        self.bus = bus
        self.model = model
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._started = False
        self._session_id: str | None = None

    @property
    def running(self) -> bool:
        return self._started

    @property
    def session_id(self) -> str | None:
        """The Claude Code session id, once the first turn has reported it.

        This is what makes `claude --resume <id>` attach a real terminal to the
        very conversation the panel is driving.
        """
        return self._session_id

    def resume_command(self) -> str | None:
        if not self._session_id:
            return None
        return f"claude --resume {self._session_id}"

    async def start(self) -> None:
        """Connect the session. Raises :class:`AgentUnavailable` if it cannot."""
        if self._started:
            return

        ok, reason = sdk_status()
        if not ok:
            raise AgentUnavailable(reason or "agent unavailable")

        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(
            cwd=str(self.config.root),
            mcp_servers={"texai": build_navigation_server(self.config, self.bus)},
            permission_mode="acceptEdits",
            allowed_tools=list(ALLOWED_TOOLS),
            disallowed_tools=list(DISALLOWED_TOOLS),
            # Pick up the project's CLAUDE.md, skills and settings, like the CLI.
            setting_sources=["user", "project"],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM_PROMPT_APPEND,
            },
            **({"model": self.model} if self.model else {}),
        )

        try:
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            self._client = None
            raise AgentUnavailable(f"Could not start the agent session: {exc}") from exc

        self._started = True
        self.bus.publish("agent_ready", model=self.model)

    async def run_turn(self, prompt: str, turn_id: str) -> TurnOutcome:
        """Send one turn and stream everything it produces onto the bus."""
        if not self._started or self._client is None:
            raise AgentUnavailable("agent session is not running")

        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}  # tool_use_id -> tool name, to label results
        outcome = TurnOutcome(ok=False)

        def emit(entry: dict[str, Any]) -> None:
            entries.append(entry)
            self.bus.publish("agent_entry", turnId=turn_id, entry=entry)

        async with self._lock:
            try:
                await self._client.query(prompt)
                async for message in self._client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                texts.append(block.text)
                                emit(transcript.text_entry(block.text))
                            elif isinstance(block, ToolUseBlock):
                                tool_names[block.id] = block.name
                                summary = summarize_tool_use(
                                    block.name, block.input or {}, self.config.root
                                )
                                tool_calls.append({"name": block.name, "summary": summary})
                                emit(
                                    transcript.tool_use_entry(
                                        block.name, summary, block.input or {}
                                    )
                                )
                            elif isinstance(block, ThinkingBlock):
                                emit(transcript.thinking_entry())
                    elif isinstance(message, UserMessage):
                        # Tool results come back as user turns, as in Claude Code.
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                emit(
                                    transcript.tool_result_entry(
                                        tool_names.get(block.tool_use_id, "tool"),
                                        block.content,
                                        bool(block.is_error),
                                    )
                                )
                    elif isinstance(message, SystemMessage):
                        entry = transcript.system_entry(message.subtype, message.data)
                        if entry is not None:  # routine bookkeeping is dropped
                            emit(entry)
                    elif isinstance(message, ResultMessage):
                        subtype = getattr(message, "subtype", None)
                        new_session = getattr(message, "session_id", None)
                        if new_session and new_session != self._session_id:
                            self._session_id = new_session
                            # Lets the UI offer `claude --resume <id>` right away.
                            self.bus.publish(
                                "agent_session",
                                sessionId=new_session,
                                resumeCommand=self.resume_command(),
                            )
                        outcome = TurnOutcome(
                            ok=subtype != "error",
                            text="".join(texts),
                            tool_calls=tool_calls,
                            entries=entries,
                            terminal_reason=getattr(message, "terminal_reason", None),
                            cost_usd=getattr(message, "total_cost_usd", None),
                            duration_ms=getattr(message, "duration_ms", None),
                            num_turns=getattr(message, "num_turns", None),
                            session_id=self._session_id,
                        )
                        emit(
                            transcript.result_entry(
                                status=subtype or "done",
                                cost_usd=outcome.cost_usd,
                                duration_ms=outcome.duration_ms,
                                num_turns=outcome.num_turns,
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
                outcome = TurnOutcome(
                    ok=False,
                    text="".join(texts),
                    tool_calls=tool_calls,
                    entries=entries,
                    error=str(exc),
                )

        if outcome.error:
            emit(transcript.notice_entry(outcome.error, is_error=True))
            self.bus.publish("agent_error", turnId=turn_id, message=outcome.error)
        return outcome

    async def interrupt(self) -> None:
        if self._started and self._client is not None:
            await self._client.interrupt()
            self.bus.publish("agent_interrupted")

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._client = None
        self._started = False
