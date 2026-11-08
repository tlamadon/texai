"""Which conversation the agent is in: the record, resuming, switching, reading back."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk
import pytest
from fastapi.testclient import TestClient

from texai import agent as agent_module
from texai import sessions
from texai.agent import AgentSession, TurnOutcome
from texai.codex_agent import CodexSession
from texai.config import AppConfig
from texai.events import EventBus
from texai.server import create_app
from texai.turns import TurnBusy, TurnController


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = (tmp_path / "project").resolve()
    root.mkdir()
    (root / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return AppConfig.create(root, root / "main.pdf")


# ---------------------------------------------------------------- the record


def test_texai_remembers_its_sessions_and_their_turns(config):
    root = config.root
    sessions.remember(root, "claude", "s1", title="Tighten the intro", turned=True)
    sessions.remember(root, "claude", "s1", title="Something later", turned=True)
    sessions.remember(root, "codex", "c1", title="Fix the table", turned=True)

    saved = {e["id"]: e for e in sessions.load(root)}
    assert saved["s1"]["turns"] == 2
    assert saved["s1"]["title"] == "Tighten the intro"  # the first one sticks
    assert sessions.last_session(root, "claude") == "s1"
    assert sessions.last_session(root, "codex") == "c1"
    assert sessions.last_session(root, "gemini") is None


def test_a_broken_record_is_an_empty_one(config):
    path = sessions.sessions_path(config.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert sessions.load(config.root) == []
    sessions.remember(config.root, "claude", "s1")
    assert json.loads(path.read_text())["sessions"][0]["id"] == "s1"


def test_the_catalogue_merges_the_agents_list_with_texais(config):
    sessions.remember(config.root, "claude", "mine", title="Shorten section 2", turned=True)
    saved = [
        {"id": "theirs", "title": "refactor the bib", "updatedAt": 2_000},
        {"id": "mine", "title": "The user marked 1 passage while reading ...", "updatedAt": 1_000},
    ]
    listed = sessions.catalogue(config.root, "claude", saved, current="mine")

    assert [s["id"] for s in listed] == ["theirs", "mine"]
    theirs, mine = listed
    assert (theirs["texai"], theirs["turns"], theirs["current"]) == (False, None, False)
    # texai's own title beats the composed prompt the agent's store kept.
    assert (mine["texai"], mine["turns"], mine["title"], mine["current"]) == (
        True,
        1,
        "Shorten section 2",
        True,
    )


def test_a_session_the_store_no_longer_has_is_not_offered(config):
    sessions.remember(config.root, "claude", "gone")
    assert sessions.catalogue(config.root, "claude", [], current=None) == []
    # ...unless the store could not be read at all: then texai's record stands in.
    assert [s["id"] for s in sessions.catalogue(config.root, "claude", None, None)] == ["gone"]


def test_past_prompts_read_as_the_user_wrote_them():
    composed = (
        "(Since your last turn: texai put option 2 in place.)\n\n"
        "The user marked 2 passages while reading `main.pdf`:\n\n[1] ...\n\n"
        "Instruction:\nMake these agree."
    )
    assert sessions.shown_prompt(composed) == "[2 passages marked] Make these agree."
    assert sessions.shown_prompt("Just a question") == "Just a question"

    joined = sessions.conversation(
        [
            {"role": "user", "text": "Hi"},
            {"role": "assistant", "text": ""},  # a thinking block, say
            {"role": "assistant", "text": "Reading."},
            {"role": "assistant", "text": "Done."},
        ]
    )
    assert joined == [
        {"role": "user", "text": "Hi"},
        {"role": "assistant", "text": "Reading.\n\nDone."},
    ]


# ---------------------------------------------------------------- Claude


async def test_claude_resumes_the_session_it_is_told_to(config, monkeypatch):
    seen = []

    class FakeClient:
        def __init__(self, options):
            seen.append(options)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(agent_module, "sdk_status", lambda: (True, None))

    session = AgentSession(config, EventBus())
    await session.start()
    assert seen[-1].resume is None

    await session.switch("abc")
    assert not session.running  # the next turn starts it, as the first did
    assert session.session_id == "abc"
    assert session.resume_command() == "claude --resume abc"
    await session.start()
    assert seen[-1].resume == "abc"

    await session.switch(None)
    assert session.session_id is None and session.resume_command() is None
    events = [e.data["sessionId"] for e in session.bus.since(0) if e.type == "agent_session"]
    assert events == ["abc", None]


async def test_claude_lists_and_reads_its_saved_sessions(config, monkeypatch):
    calls = {}

    def list_sessions(directory, include_worktrees):
        calls["list"] = (directory, include_worktrees)
        return [
            SimpleNamespace(
                session_id="s1",
                summary="Tighten it",
                custom_title=None,
                created_at=1,
                last_modified=2,
            )
        ]

    def get_session_messages(session_id, directory):
        return [
            SimpleNamespace(type="user", message={"content": "Tighten it"}),
            SimpleNamespace(
                type="assistant",
                message={
                    "content": [
                        {"type": "thinking", "thinking": "hm"},
                        {"type": "text", "text": "Done."},
                        {"type": "tool_use", "name": "Edit"},
                    ]
                },
            ),
            # A tool result rides in a user message, and has no prose.
            SimpleNamespace(type="user", message={"content": [{"type": "tool_result"}]}),
        ]

    monkeypatch.setattr(claude_agent_sdk, "list_sessions", list_sessions)
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", get_session_messages)
    session = AgentSession(config, EventBus())

    assert await session.saved_sessions() == [
        {"id": "s1", "title": "Tighten it", "customTitle": None, "createdAt": 1, "updatedAt": 2}
    ]
    assert calls["list"] == (str(config.root), False)
    messages = sessions.conversation(await session.past_messages("s1"))
    assert messages == [
        {"role": "user", "text": "Tighten it"},
        {"role": "assistant", "text": "Done."},
    ]


# ---------------------------------------------------------------- Codex


class FakeCodex:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def thread_start(self, **settings):
        self.log.append(("start", None, settings))
        return SimpleNamespace(id="fresh")

    async def thread_resume(self, thread_id, **settings):
        self.log.append(("resume", thread_id, settings))
        return SimpleNamespace(id=thread_id)

    async def thread_list(self, **kwargs):
        self.log.append(("list", None, kwargs))
        return SimpleNamespace(
            data=[
                {"id": "t1", "preview": "Fix the table", "created_at": 10, "updated_at": 20},
                {"id": "sub", "preview": "x", "parent_thread_id": "t1"},
            ]
        )


class FakeAsyncThread:
    def __init__(self, codex, thread_id):
        self.id = thread_id

    async def read(self, include_turns=False):
        turn = {
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "Fix the table"}]},
                {"type": "reasoning"},
                {"type": "agentMessage", "text": "Fixed."},
            ]
        }
        return SimpleNamespace(thread={"turns": [turn]})


@pytest.fixture()
def fake_codex(monkeypatch):
    log: list = []
    module = SimpleNamespace(
        AsyncCodex=lambda: FakeCodex(log),
        AsyncThread=FakeAsyncThread,
        ApprovalMode=SimpleNamespace(deny_all="deny"),
        Sandbox=SimpleNamespace(workspace_write="write"),
    )
    monkeypatch.setitem(sys.modules, "openai_codex", module)
    monkeypatch.setattr(CodexSession, "status", lambda self: (True, None))
    return log


async def test_codex_resumes_a_thread_with_texais_settings(config, fake_codex):
    session = CodexSession(config, EventBus())
    session.resume("t1")
    await session.start()
    kind, thread_id, settings = fake_codex[-1]
    assert (kind, thread_id, session.session_id) == ("resume", "t1", "t1")
    assert settings["developer_instructions"]  # texai's instructions still apply

    await session.switch(None)
    await session.start()
    assert fake_codex[-1][0] == "start" and session.session_id == "fresh"


async def test_codex_lists_and_reads_its_threads(config, fake_codex):
    session = CodexSession(config, EventBus())
    listed = await session.saved_sessions()
    assert listed == [
        {"id": "t1", "title": "Fix the table", "customTitle": None, "createdAt": 10_000, "updatedAt": 20_000}
    ]
    assert fake_codex[-1][2]["cwd"] == str(config.root)
    assert sessions.conversation(await session.past_messages("t1")) == [
        {"role": "user", "text": "Fix the table"},
        {"role": "assistant", "text": "Fixed."},
    ]


# ---------------------------------------------------------------- controller and API


class SessionAgent:
    """An agent with saved conversations to switch between."""

    kind = "claude"
    name = "Claude"

    def __init__(self, bus):
        self.bus = bus
        self.running = False
        self.session_id = None
        self.saved = [
            {"id": "s1", "title": "Tighten it", "createdAt": 1, "updatedAt": 2},
            {"id": "s2", "title": "From a terminal", "createdAt": 1, "updatedAt": 3},
        ]
        self.gate: asyncio.Event | None = None

    def status(self):
        return True, None

    def resume_command(self):
        return f"claude --resume {self.session_id}" if self.session_id else None

    async def start(self):
        self.running = True

    async def run_turn(self, prompt, turn_id):
        if self.gate is not None:
            await self.gate.wait()
        self.session_id = self.session_id or "s1"
        return TurnOutcome(ok=True, text="Sure.", session_id=self.session_id)

    async def switch(self, session_id):
        self.running = False
        self.session_id = session_id

    async def saved_sessions(self):
        return self.saved

    async def past_messages(self, session_id):
        return [{"role": "user", "text": "Tighten it"}, {"role": "assistant", "text": "Done."}]

    async def interrupt(self):
        pass

    async def stop(self):
        self.running = False


async def test_turns_record_their_conversation(config):
    bus = EventBus()
    controller = TurnController(config, bus, SessionAgent(bus))
    turn = await controller.submit("What does this mean?", [])
    await controller._task
    assert turn.session_id == "s1" and turn.as_dict()["sessionId"] == "s1"
    (entry,) = sessions.load(config.root)
    assert (entry["id"], entry["turns"], entry["title"]) == ("s1", 1, "What does this mean?")


async def test_switching_waits_for_the_running_turn(config):
    bus = EventBus()
    agent = SessionAgent(bus)
    agent.gate = asyncio.Event()
    controller = TurnController(config, bus, agent)
    await controller.submit("Tighten it", [])
    with pytest.raises(TurnBusy):
        await controller.switch_session("s2")
    agent.gate.set()
    await controller._task

    await controller.switch_session("s2")
    assert agent.session_id == "s2"
    assert [e.data["sessionId"] for e in bus.since(0) if e.type == "session_switched"] == ["s2"]


def test_the_panel_lists_switches_and_reads_back(config):
    agent = SessionAgent(EventBus())
    sessions.remember(config.root, "claude", "s1", title="Tighten the intro", turned=True)
    client = TestClient(create_app(config, agent=agent), base_url="http://127.0.0.1")

    listed = client.get("/api/sessions").json()
    assert listed["current"] is None
    assert [(s["id"], s["texai"]) for s in listed["sessions"]] == [("s2", False), ("s1", True)]

    assert client.post("/api/sessions/switch", json={"sessionId": "nope"}).status_code == 404
    assert client.post("/api/sessions/switch", json={"sessionId": "s2"}).json()["ok"]
    assert agent.session_id == "s2"
    assert client.get("/api/sessions").json()["current"] == "s2"

    messages = client.get("/api/sessions/s1/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert client.get("/api/sessions/nope/messages").status_code == 404

    assert client.post("/api/sessions/switch", json={"sessionId": None}).json()["ok"]
    assert agent.session_id is None


async def test_a_conversation_taken_over_keeps_its_own_title(config):
    bus = EventBus()
    agent = SessionAgent(bus)
    controller = TurnController(config, bus, agent)
    await controller.switch_session("s2")
    await controller.submit("Now tighten the intro", [])
    await controller._task

    listed = sessions.catalogue(config.root, "claude", agent.saved, current="s2")
    taken = next(s for s in listed if s["id"] == "s2")
    assert (taken["texai"], taken["turns"], taken["title"]) == (True, 1, "From a terminal")
