"""Codex as the editing agent: its events, its session, and texai's tool endpoint.

None of this needs a Codex login. The translator is fed plain dicts shaped like
the SDK's notification payloads, the session runs against a stand-in thread, and
the MCP endpoint is exercised over HTTP the way Codex reaches it.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from texai import codex_agent
from texai.agent import AgentSession, create_agent
from texai.agent_tools import TOOLS_PATH
from texai.codex_agent import CodexSession, CodexTranscript, _thread_config
from texai.config import AppConfig
from texai.events import EventBus
from texai.server import create_app


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = (tmp_path / "project").resolve()
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    (root / "sections" / "model.tex").write_text("\\section{Model}\n")
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return AppConfig.create(root, root / "main.pdf")


# ---------------------------------------------------------------- translation


def command(status="completed", exit_code=0, output="12:Equation\n"):
    return {
        "type": "commandExecution",
        "id": "cmd-1",
        "command": "/bin/zsh -lc \"rg -n Equation sections/model.tex\"",
        "command_actions": [{"command": "rg -n Equation sections/model.tex"}],
        "status": status,
        "exit_code": exit_code,
        "aggregated_output": output,
    }


def test_a_command_shows_as_typed_when_it_starts_and_its_output_when_done(config):
    log = CodexTranscript(config.root)
    started = log.feed("item/started", {"item": command(status="inProgress")})
    done = log.feed("item/completed", {"item": command()})

    assert [(e["kind"], e["verb"], e["text"]) for e in started] == [
        ("tool_use", "Shell", "rg -n Equation sections/model.tex")
    ]
    assert [(e["kind"], e.get("isError")) for e in done] == [("tool_result", None)]
    assert log.tool_calls == [{"name": "Shell", "summary": "rg -n Equation sections/model.tex"}]


def test_a_compound_command_is_shown_without_its_shell_launcher(config):
    item = {
        "type": "commandExecution",
        "id": "cmd-2",
        "command": "/run/current-system/sw/bin/zsh -lc \"rg --files sections; sed -n 1,5p main.tex\"",
        "command_actions": [{"command": "rg --files sections"}, {"command": "sed -n 1,5p main.tex"}],
    }
    (entry,) = CodexTranscript(config.root).feed("item/started", {"item": item})
    assert entry["text"] == "rg --files sections; sed -n 1,5p main.tex"


def test_a_nonzero_exit_is_reported_but_not_an_error(config):
    # rg exits 1 when nothing matches; that is an answer, not a failure.
    log = CodexTranscript(config.root)
    (entry,) = log.feed("item/completed", {"item": command(exit_code=1, output="")})[1:]
    assert entry["text"].startswith("exit 1")
    assert entry.get("isError") is None


def test_a_patch_names_the_files_relative_to_the_root(config):
    item = {
        "type": "fileChange",
        "id": "patch-1",
        "status": "completed",
        "changes": [{"path": str(config.root / "sections" / "model.tex"), "diff": "@@"}],
    }
    entries = CodexTranscript(config.root).feed("item/completed", {"item": item})
    # Seen only at completion: the tool line is still emitted, then its result.
    assert [(e["kind"], e["verb"], e["text"]) for e in entries] == [
        ("tool_use", "Edit", "sections/model.tex"),
        ("tool_result", "Edit", "1 file(s) changed"),
    ]


def test_messages_reasoning_and_tool_calls(config):
    log = CodexTranscript(config.root)
    tool = {
        "type": "mcpToolCall",
        "id": "mcp-1",
        "server": "texai",
        "tool": "show_in_pdf",
        "arguments": {"file": "sections/model.tex", "line": 3, "why": "the model"},
        "status": "completed",
        "result": {"content": [{"type": "text", "text": "Showing sections/model.tex:3"}]},
    }
    entries = [
        *log.feed("item/completed", {"item": {"type": "reasoning", "id": "r"}}),
        *log.feed("item/started", {"item": {**tool, "status": "inProgress"}}),
        *log.feed("item/completed", {"item": tool}),
        *log.feed("item/completed", {"item": {"type": "agentMessage", "text": "Done."}}),
        *log.feed("item/completed", {"item": {"type": "userMessage", "content": []}}),
        *log.feed("item/agentMessage/delta", {"delta": "Do"}),
    ]
    assert [e["kind"] for e in entries] == ["thinking", "tool_use", "tool_result", "text"]
    assert entries[1]["text"] == "sections/model.tex:3"
    assert log.texts == ["Done."]


def test_a_failed_tool_call_carries_its_error(config):
    tool = {
        "type": "mcpToolCall",
        "id": "mcp-2",
        "tool": "show_in_pdf",
        "status": "failed",
        "error": {"message": "MCP tool call requires approval"},
    }
    entries = CodexTranscript(config.root).feed("item/completed", {"item": tool})
    assert entries[-1]["isError"] is True
    assert "requires approval" in entries[-1]["text"]


def test_only_final_errors_become_notices(config):
    log = CodexTranscript(config.root)
    assert log.feed("error", {"error": {"message": "overloaded"}, "will_retry": True}) == []
    (notice,) = log.feed("error", {"error": {"message": "quota exceeded"}, "will_retry": False})
    assert notice["kind"] == "notice" and notice["isError"] is True


def test_the_thread_is_pointed_at_texai_with_its_tool_pre_approved():
    # Approvals are denied for the thread, so without this every call is refused.
    texai = _thread_config("http://127.0.0.1:8765/agent-tools/mcp")["mcp_servers"]["texai"]
    assert texai["url"].endswith(TOOLS_PATH)
    assert texai["tools"]["show_in_pdf"]["approval_mode"] == "approve"
    assert _thread_config(None) is None


# ---------------------------------------------------------------- the session


class FakeTurn:
    def __init__(self, notifications):
        self.notifications = notifications
        self.interrupted = False

    async def stream(self):
        for method, params in self.notifications:
            await asyncio.sleep(0)
            yield SimpleNamespace(method=method, payload=SimpleNamespace(params=params))

    async def interrupt(self):
        self.interrupted = True


class FakeThread:
    id = "thread-123"

    def __init__(self, notifications):
        self.notifications = notifications
        self.prompts = []

    async def turn(self, prompt):
        self.prompts.append(prompt)
        return FakeTurn(self.notifications)


def running_session(config, notifications) -> CodexSession:
    session = CodexSession(config, EventBus())
    session._thread = FakeThread(notifications)
    session._started = True
    session._session_id = FakeThread.id
    return session


async def test_a_turn_streams_onto_the_bus_and_reports_its_outcome(config):
    session = running_session(
        config,
        [
            ("item/completed", {"item": {"type": "agentMessage", "text": "Tightened it."}}),
            ("turn/completed", {"turn": {"status": "completed", "duration_ms": 2500}}),
        ],
    )
    outcome = await session.run_turn("tighten this", "t1")

    assert outcome.ok and outcome.error is None
    assert outcome.text == "Tightened it."
    assert [e["kind"] for e in outcome.entries] == ["text", "result"]
    assert outcome.entries[-1]["text"] == "success · 2.5s"
    published = [e.type for e in session.bus.since(0)]
    assert published.count("agent_entry") == 2
    assert session.resume_command() == "codex resume thread-123"


async def test_a_failed_turn_is_an_error_the_controller_can_see(config):
    session = running_session(
        config,
        [("turn/completed", {"turn": {"status": "failed", "error": {"message": "usage limit"}}})],
    )
    outcome = await session.run_turn("tighten this", "t1")
    assert not outcome.ok
    assert outcome.error == "usage limit"
    assert "agent_error" in [e.type for e in session.bus.since(0)]


def test_the_agent_is_chosen_by_name(config):
    bus = EventBus()
    assert isinstance(create_agent("claude", config, bus), AgentSession)
    codex = create_agent("codex", config, bus, model="gpt-5.4", tools_url="http://x/mcp")
    assert (codex.kind, codex.model, codex.tools_url) == ("codex", "gpt-5.4", "http://x/mcp")
    with pytest.raises(ValueError, match="unknown agent"):
        create_agent("gemini", config, bus)


def test_a_missing_sdk_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "openai_codex", None)  # import now fails
    ok, reason = codex_agent.codex_status()
    assert ok is False and "texai[codex]" in reason


# ---------------------------------------------------------------- tool endpoint


def _rpc(client, payload, session_id=None):
    headers = {"accept": "application/json, text/event-stream"}
    if session_id:
        headers["mcp-session-id"] = session_id
    response = client.post(TOOLS_PATH, json=payload, headers=headers)
    body = response.text
    # Streamable HTTP may answer as an SSE stream of one message.
    if "data:" in body:
        body = next(line[5:] for line in body.splitlines() if line.startswith("data:"))
    return response, (json.loads(body) if body.strip() else None)


def test_codex_reaches_show_in_pdf_on_texais_own_server(config):
    app = create_app(config, agent=CodexSession(config, EventBus()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response, _ = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        session_id = response.headers.get("mcp-session-id")
        _rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)

        _, listed = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id)
        assert [tool["name"] for tool in listed["result"]["tools"]] == ["show_in_pdf"]

        _, called = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "show_in_pdf", "arguments": {"file": "sections/model.tex", "line": 1}},
            },
            session_id,
        )
        # No SyncTeX data in this project, so the view cannot move — but the
        # call went all the way through to the same handler Claude uses.
        text = called["result"]["content"][0]["text"]
        assert "sections/model.tex" in text or "Could not show that" in text

        info = client.get("/api/info").json()["agent"]
        assert (info["kind"], info["name"]) == ("codex", "Codex")


def test_claude_gets_no_tool_endpoint(config):
    with TestClient(create_app(config), base_url="http://127.0.0.1") as client:
        assert client.post(TOOLS_PATH, json={}).status_code in (404, 405)
        assert client.get("/api/info").json()["agent"]["kind"] == "claude"
