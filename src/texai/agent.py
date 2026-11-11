"""The background agent session that edits the document.

One long-lived session per project, so the agent accumulates knowledge of the
paper across turns instead of re-reading it every time. Everything it emits is
republished on the event bus, which is what the chat panel renders.

Two agents can sit behind it: Claude (the default, this module) and Codex
(:mod:`texai.codex_agent`). Both answer to the same small interface — start,
run a turn, interrupt, stop, a session id and a resume command — and report a
:class:`TurnOutcome`, so the turn controller never knows which one it drives.
Everything that keeps a turn honest (snapshots, the rebuild, revert) works from
the files on disk, not from what the agent says it did, so it holds for either.

When an agent cannot run, the viewer, SyncTeX bridge and selection file all
still work, and the chat panel reports why it is disabled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import transcript
from .config import AppConfig
from .timing import AgentClock
from .events import EventBus
from .navigate import LocateError, locate
from .options import OFFER_OPTIONS_DESCRIPTION, OFFER_OPTIONS_SCHEMA, offer_options
from .papers import READ_PAPER_DESCRIPTION, READ_PAPER_SCHEMA, read_paper
from .record_tools import (
    ADD_SOURCE_DESCRIPTION,
    ANNOTATIONS_DESCRIPTION,
    COMMENT_DESCRIPTION,
    add_source,
    annotations_for,
    comment,
)
from .prompt import SYSTEM_PROMPT_APPEND

__all__ = [
    "AGENT_KINDS",
    "EFFORT_LEVELS",
    "AgentUnavailable",
    "TurnOutcome",
    "AgentSession",
    "agent_status",
    "create_agent",
    "navigate_to",
    "sdk_status",
]

# Which agents texai can drive. The first is the default.
AGENT_KINDS = ("claude", "codex")

# How hard Claude thinks before answering (the SDK's ``effort``). None leaves
# it to Claude Code's own default.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Everything the agent needs to edit LaTeX. Bash is deliberately absent: the
# harness owns compilation, so a shell would only add risk and thrash.
#
# These are also the only built-in tools the session is given, and texai's are
# the only MCP tools: every tool the model can see rides along in every
# request, and past a few dozen Claude Code starts holding them back behind a
# search, so reaching show_in_pdf would cost a model call of its own. The
# user's own MCP servers and connectors are left out for the same reason.
BUILTIN_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
]
ALLOWED_TOOLS = [
    *BUILTIN_TOOLS,
    # In-process tool that scrolls the user's PDF view (see build_navigation_server).
    "mcp__texai__show_in_pdf",
    # Versions of a passage for the user to pick from (see texai.options).
    "mcp__texai__offer_options",
    # A PDF as text, and where a quote is in it (see texai.papers).
    "mcp__texai__read_paper",
    # Records in annotations.jsonl: read them, add a source, comment.
    "mcp__texai__annotations",
    "mcp__texai__add_source",
    "mcp__texai__comment",
]
DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Task", "KillShell", "BashOutput"]

INSTALL_HINT = (
    "The Claude Agent SDK is missing. It ships with texai, so the environment "
    "is incomplete — run `uv sync` (or `pip install --upgrade texai`) and restart."
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
    # Where the call's time went (see texai.timing).
    timing: dict[str, Any] | None = None


def sdk_status() -> tuple[bool, str | None]:
    """Whether the Claude agent can run here, and why not if it cannot."""
    import shutil

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False, INSTALL_HINT
    if shutil.which("claude") is None:
        return False, CLI_HINT
    return True, None


def agent_status(kind: str = "claude") -> tuple[bool, str | None]:
    """Whether the named agent can run here, and why not if it cannot."""
    if kind == "codex":
        from .codex_agent import codex_status

        return codex_status()
    return sdk_status()


def create_agent(
    kind: str,
    config: AppConfig,
    bus: EventBus,
    model: str | None = None,
    tools_url: str | None = None,
    effort: str | None = None,
) -> Any:
    """The session for the named agent.

    ``tools_url`` is where texai serves its own tools over MCP. Claude takes them
    in-process and does not need it; Codex runs as a separate process and
    reaches them there.
    """
    if kind == "codex":
        from .codex_agent import CodexSession

        return CodexSession(config, bus, model=model, tools_url=tools_url, effort=effort)
    if kind != "claude":
        raise ValueError(f"unknown agent {kind!r}; expected one of {', '.join(AGENT_KINDS)}")
    return AgentSession(config, bus, model=model, effort=effort)


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
    if name.endswith("offer_options"):
        count = len(tool_input.get("options") or [])
        return f"{count} versions of a passage in {tool_input.get('file', '')}".strip()
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


async def navigate_to(config: AppConfig, bus: EventBus, file: Any, line: Any, why: Any) -> str:
    """Move the user's view to a source location; the tool's answer, as text.

    The body of ``show_in_pdf`` for every agent: Claude calls it in-process,
    Codex over texai's MCP endpoint.
    """
    file = str(file or "").strip()
    try:
        line = int(line or 1)
    except (TypeError, ValueError):
        line = 1
    why = str(why or "").strip()

    try:
        found = await asyncio.to_thread(locate, config, file, line)
    except LocateError as exc:
        return f"Could not show that: {exc}"

    if not found["found"]:
        return (
            f"SyncTeX has no record of {found['file']}:{found['line']}, so the "
            "view did not move. That line may produce no output (a comment, a "
            "definition, or something inside an untracked environment) — try a "
            "line that renders visible text."
        )

    bus.publish("navigate", **found, why=why)
    return f"Showing {found['file']}:{found['line']} — the view moved to page {found['page']}."


def build_navigation_server(config: AppConfig, bus: EventBus) -> Any:
    """An in-process MCP server giving the agent texai's own actions: move the
    view, offer versions of a passage for the user to pick from, and read a
    paper's PDF as text.

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
        text = await navigate_to(
            config, bus, args.get("file", ""), args.get("line", 1), args.get("why", "")
        )
        return {"content": [{"type": "text", "text": text}]}

    @tool("offer_options", OFFER_OPTIONS_DESCRIPTION, OFFER_OPTIONS_SCHEMA)
    async def offer(args: dict[str, Any]) -> dict[str, Any]:
        text = await offer_options(
            config,
            bus,
            args.get("file", ""),
            args.get("original", ""),
            args.get("options", []),
            args.get("note", ""),
        )
        return {"content": [{"type": "text", "text": text}]}

    @tool("read_paper", READ_PAPER_DESCRIPTION, READ_PAPER_SCHEMA)
    async def paper(args: dict[str, Any]) -> dict[str, Any]:
        text = await asyncio.to_thread(
            read_paper,
            config,
            args.get("target", ""),
            args.get("pages", "") or "",
            args.get("find", "") or "",
        )
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "annotations",
        ANNOTATIONS_DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "to_line": {"type": "integer"},
            },
            "required": ["file", "line"],
        },
    )
    async def attached(args: dict[str, Any]) -> dict[str, Any]:
        text = await asyncio.to_thread(
            annotations_for, config, args.get("file", ""), args.get("line", 1), args.get("to_line")
        )
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "add_source",
        ADD_SOURCE_DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "target": {"type": "string"},
                "page": {"type": "integer"},
                "quote": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["file", "line", "target"],
        },
    )
    async def source(args: dict[str, Any]) -> dict[str, Any]:
        text = await asyncio.to_thread(
            add_source,
            config,
            args.get("file", ""),
            args.get("line", 0),
            args.get("target", ""),
            args.get("page"),
            args.get("quote", "") or "",
            args.get("note", "") or "",
        )
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "comment",
        COMMENT_DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "text": {"type": "string"},
                "reply_to": {"type": "string"},
            },
            "required": ["file", "text"],
        },
    )
    async def note(args: dict[str, Any]) -> dict[str, Any]:
        text = await asyncio.to_thread(
            comment,
            config,
            args.get("file", ""),
            args.get("line", 0),
            args.get("text", ""),
            args.get("reply_to", "") or "",
        )
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(
        name="texai",
        version="1.0.0",
        tools=[show_in_pdf, offer, paper, attached, source, note],
    )


def _block_kind(content: list[Any], thinking: type, tool_use: type) -> str:
    """What a model message was spent on, for the clock: the first block decides."""
    for block in content:
        if isinstance(block, thinking):
            return "thinking"
        if isinstance(block, tool_use):
            return "tool_use"
        return "text"
    return "text"


def _message_text(content: Any) -> str:
    """The prose of a saved message: its text blocks, not its tool traffic."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _init_facts(data: Any) -> dict[str, Any]:
    """What the session start-up says about the request size: model and tool surface."""
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcp_servers") or []
    return {
        "initModel": data.get("model"),
        "toolCount": len(data.get("tools") or []),
        "mcpServers": [
            s.get("name") if isinstance(s, dict) else str(s) for s in servers
        ],
    }


def _result_facts(message: Any) -> dict[str, Any]:
    """Claude Code's own account of the call: API time and tokens."""
    usage = getattr(message, "usage", None) or {}
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {
        "durationMs": getattr(message, "duration_ms", None),
        "apiMs": getattr(message, "duration_api_ms", None),
        "numTurns": getattr(message, "num_turns", None),
        "costUsd": getattr(message, "total_cost_usd", None),
        "usage": {k: usage[k] for k in keys if isinstance(usage, dict) and k in usage},
    }


class AgentSession:
    """A single persistent Claude conversation scoped to one project."""

    kind = "claude"
    name = "Claude"
    # Whether this is the session the chat panel drives: it announces itself,
    # offers `claude --resume`, and its replies are shown as written.
    primary = True

    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.model = model
        self.effort = effort
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._started = False
        self._session_id: str | None = None
        # The conversation the next start picks up, rather than a fresh one.
        self._resume: str | None = None

    # What the panel offers to choose from. The models are Claude Code's own
    # aliases, which follow the current release of each; any other id it
    # accepts can be typed in.
    models: tuple[str, ...] = ("opus", "sonnet", "haiku")
    efforts: tuple[str, ...] = EFFORT_LEVELS

    @property
    def running(self) -> bool:
        return self._started

    def status(self) -> tuple[bool, str | None]:
        return sdk_status()

    async def configure(self, model: str | None, effort: str | None) -> None:
        """Change the model and effort (None: the agent's own default).

        Both are fixed when a session connects, so this one is closed; the next
        turn reconnects to the same conversation with the new settings.
        """
        if (model, effort) == (self.model, self.effort):
            return
        session = self.session_id
        await self.stop()
        self.model, self.effort = model, effort
        if session:
            self.resume(session)

    def resume(self, session_id: str | None) -> None:
        """Make the next start continue this conversation (None: a new one)."""
        self._resume = session_id
        self._session_id = session_id

    async def switch(self, session_id: str | None) -> None:
        """Leave the current conversation for another, or for a fresh one.

        The new one starts lazily, with the next turn, like the first did.
        """
        await self.stop()
        self.resume(session_id)
        self.bus.publish(
            "agent_session", sessionId=session_id, resumeCommand=self.resume_command()
        )

    async def saved_sessions(self) -> list[dict[str, Any]] | None:
        """Claude Code's saved conversations for this project, newest first."""
        try:
            from claude_agent_sdk import list_sessions

            found = await asyncio.to_thread(
                list_sessions, directory=str(self.config.root), include_worktrees=False
            )
        except Exception:  # noqa: BLE001 - texai's own record stands in
            return None
        return [
            {
                "id": s.session_id,
                "title": s.summary,
                "customTitle": s.custom_title,
                "createdAt": s.created_at,
                "updatedAt": s.last_modified,
            }
            for s in found
        ]

    async def past_messages(self, session_id: str) -> list[dict[str, Any]]:
        """What was said in a saved conversation, as ``{role, text}``."""
        from claude_agent_sdk import get_session_messages

        found = await asyncio.to_thread(
            get_session_messages, session_id, directory=str(self.config.root)
        )
        return [
            {"role": m.type, "text": _message_text((m.message or {}).get("content"))}
            for m in found
        ]

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

        ok, reason = self.status()
        if not ok:
            raise AgentUnavailable(reason or "agent unavailable")

        from claude_agent_sdk import ClaudeSDKClient

        try:
            self._client = ClaudeSDKClient(options=self._options())
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            self._client = None
            raise AgentUnavailable(f"Could not start the agent session: {exc}") from exc

        self._started = True
        if self.primary:
            self.bus.publish("agent_ready", model=self.model)

    def _options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            cwd=str(self.config.root),
            mcp_servers={"texai": build_navigation_server(self.config, self.bus)},
            strict_mcp_config=True,
            tools=list(BUILTIN_TOOLS),
            # The raw API stream, so each model request can be timed from the
            # inside (see texai.timing); the transcript still comes from the
            # finished messages.
            include_partial_messages=True,
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
            **({"effort": self.effort} if self.effort else {}),
            **({"resume": self._resume} if self._resume else {}),
        )

    async def run_turn(self, prompt: str, turn_id: str) -> TurnOutcome:
        """Send one turn and stream everything it produces onto the bus."""
        if not self._started or self._client is None:
            raise AgentUnavailable("agent session is not running")

        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
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
        clock = AgentClock()

        def emit(entry: dict[str, Any]) -> None:
            entries.append(entry)
            self.bus.publish("agent_entry", turnId=turn_id, entry=entry)

        async with self._lock:
            try:
                await self._client.query(prompt)
                async for message in self._client.receive_response():
                    if isinstance(message, StreamEvent):
                        clock.stream(message.event)
                    elif isinstance(message, AssistantMessage):
                        clock.model(
                            _block_kind(message.content, ThinkingBlock, ToolUseBlock),
                            call=getattr(message, "message_id", None),
                            model=getattr(message, "model", None),
                        )
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                texts.append(block.text)
                                if self.primary:
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
                        results = [
                            tool_names.get(block.tool_use_id, "tool")
                            for block in message.content
                            if isinstance(block, ToolResultBlock)
                        ]
                        if results:
                            clock.tool("+".join(dict.fromkeys(results)))
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
                        if message.subtype == "thinking_tokens":
                            clock.thinking()  # sent while the model thinks
                        else:
                            clock.harness(message.subtype)
                        if message.subtype == "init":
                            clock.extra.update(_init_facts(message.data))
                        entry = transcript.system_entry(message.subtype, message.data)
                        if entry is not None:  # routine bookkeeping is dropped
                            emit(entry)
                    elif isinstance(message, ResultMessage):
                        clock.harness("result")
                        clock.extra.update(_result_facts(message))
                        subtype = getattr(message, "subtype", None)
                        new_session = getattr(message, "session_id", None)
                        if self.primary and new_session and new_session != self._session_id:
                            self._session_id = new_session
                            self._resume = new_session  # a restart carries on here
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

        outcome.timing = clock.summary()
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
