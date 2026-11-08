"""Where a turn's time goes: the agent clock, the Claude session feeding it, the log."""

import json
from pathlib import Path

import pytest
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

from texai import timing
from texai.agent import AgentSession
from texai.codex_agent import tick
from texai.config import AppConfig
from texai.events import EventBus
from texai.timing import AgentClock, record, timings_path


class FakeTime:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(timing.time, "monotonic", fake)
    return fake


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    (tmp_path / "main.tex").write_text("\\begin{document}\nhi\n\\end{document}\n")
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return AppConfig.create(tmp_path, tmp_path / "main.pdf")


def test_the_time_before_each_arrival_is_charged_to_it(clock_time: FakeTime):
    clock = AgentClock()
    clock_time.advance(0.5)
    clock.harness("init")
    clock_time.advance(3.0)
    clock.model("thinking", call="m1", model="claude-x")
    clock_time.advance(1.0)
    clock.model("tool_use", call="m1", model="claude-x")
    clock_time.advance(0.2)
    clock.tool("Read")
    clock_time.advance(2.0)
    clock.model("text", call="m2", model="claude-x")

    summary = clock.summary()
    assert summary["modelMs"] == 6000
    assert summary["thinkingMs"] == 3000
    assert summary["firstBlockMs"] == 5000  # 3.0s into m1, 2.0s into m2
    assert summary["toolMs"] == 200 and summary["tools"] == {"Read": 200}
    assert summary["harnessMs"] == 500
    assert summary["modelCalls"] == 2
    assert summary["models"] == ["claude-x"]
    assert summary["wallMs"] == 6700


def test_without_call_ids_a_tool_result_starts_a_new_model_call(clock_time: FakeTime):
    clock = AgentClock()
    clock.model("thinking")
    clock.model("tool_use")
    clock.tool("Shell")
    clock.model("text")
    assert clock.summary()["modelCalls"] == 2


def test_codex_items_are_charged_to_the_model_or_the_tool(clock_time: FakeTime):
    clock = AgentClock()
    clock_time.advance(2.0)
    tick(clock, "item/completed", {"item": {"type": "reasoning"}})
    clock_time.advance(0.5)
    tick(clock, "item/started", {"item": {"type": "commandExecution"}})
    clock_time.advance(0.3)
    tick(clock, "item/completed", {"item": {"type": "commandExecution"}})
    clock_time.advance(1.0)
    tick(clock, "item/completed", {"item": {"type": "agentMessage", "text": "done"}})

    summary = clock.summary()
    assert summary["thinkingMs"] == 2000
    assert summary["modelMs"] == 3500
    assert summary["tools"] == {"Shell": 300}
    assert summary["modelCalls"] == 2


class FakeClient:
    """Plays back a scripted message stream, moving the clock between messages."""

    def __init__(self, clock_time: FakeTime, script: list[tuple[float, object]]) -> None:
        self.clock_time = clock_time
        self.script = script

    async def query(self, prompt: str) -> None:
        pass

    async def receive_response(self):
        for delay, message in self.script:
            self.clock_time.advance(delay)
            yield message


async def test_a_claude_turn_reports_where_its_time_went(config: AppConfig, clock_time: FakeTime):
    script = [
        (0.4, SystemMessage("init", {"model": "claude-x", "tools": ["Read", "Edit"], "mcp_servers": [{"name": "texai"}]})),
        (2.0, AssistantMessage([ThinkingBlock("…", "sig")], "claude-x", message_id="m1")),
        (1.0, AssistantMessage([ToolUseBlock("u1", "Edit", {"file_path": "main.tex"})], "claude-x", message_id="m1")),
        (0.1, UserMessage([ToolResultBlock("u1", "ok")])),
        (1.5, AssistantMessage([TextBlock("Edited main.tex.")], "claude-x", message_id="m2")),
        (
            0.0,
            ResultMessage(
                "success", 5000, 4400, False, 2, "s1",
                usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 9000},
            ),
        ),
    ]
    session = AgentSession(config, EventBus(), effort="low")
    session._client = FakeClient(clock_time, script)
    session._started = True

    outcome = await session.run_turn("tighten", "t0001")

    summary = outcome.timing
    assert summary["modelMs"] == 4500 and summary["thinkingMs"] == 2000
    assert summary["tools"] == {"Edit": 100}
    assert summary["modelCalls"] == 2
    assert summary["initModel"] == "claude-x"
    assert summary["toolCount"] == 2 and summary["mcpServers"] == ["texai"]
    assert summary["apiMs"] == 4400
    assert summary["usage"]["cache_read_input_tokens"] == 9000


def test_records_are_appended_one_json_line_each(tmp_path: Path):
    record(tmp_path, {"kind": "completion", "totalMs": 300})
    record(tmp_path, {"kind": "turn", "totalMs": 9000})
    lines = timings_path(tmp_path).read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["completion", "turn"]


def test_thinking_progress_is_model_time_and_one_call_with_its_answer(clock_time: FakeTime):
    clock = AgentClock()
    clock.tool("Edit")
    clock_time.advance(1.3)
    clock.thinking()
    clock_time.advance(1.4)
    clock.thinking()
    clock_time.advance(0.5)
    clock.model("tool_use", call="m3")

    summary = clock.summary()
    assert summary["thinkingMs"] == 2700
    assert summary["modelMs"] == 3200
    assert summary["firstBlockMs"] == 1300
    assert summary["modelCalls"] == 1


def test_the_raw_stream_splits_a_request_into_wait_and_generation(
    clock_time: FakeTime,
):
    clock = AgentClock()
    clock_time.advance(1.2)  # the agent's overhead, the network, reading the prompt
    clock.stream(
        {
            "type": "message_start",
            "message": {"id": "m1", "usage": {"input_tokens": 3, "cache_read_input_tokens": 9000}},
        }
    )
    clock.stream({"type": "content_block_start", "content_block": {"type": "text"}})
    clock.stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}})
    clock_time.advance(0.8)
    clock.stream({"type": "content_block_stop"})
    clock.stream({"type": "message_delta", "usage": {"output_tokens": 40}})
    clock.stream({"type": "message_stop"})
    clock_time.advance(0.1)
    clock.tool("Edit")
    clock_time.advance(0.7)
    clock.stream({"type": "message_start", "message": {"id": "m2", "usage": {}}})

    first, second = clock.summary()["requests"]
    assert first["waitMs"] == 1200
    assert first["generateMs"] == 800 and first["tokensPerS"] == 50.0
    assert first["blocks"] == [{"type": "text", "ms": 800}]
    assert first["cacheReadTokens"] == 9000
    assert second["waitMs"] == 700  # from the tool result, not the end of m1


def test_the_estimate_follows_recent_turns_that_went_the_same_way(tmp_path: Path):
    from texai.timing import DEFAULT_BUILD_MS, DEFAULT_WORK_MS, estimate_turn

    fresh = estimate_turn(tmp_path, "quick")
    assert fresh["phases"] == [
        {"name": "writing", "ms": DEFAULT_WORK_MS[("quick", False)]},
        {"name": "compiling", "ms": DEFAULT_BUILD_MS},
    ]
    assert fresh["samples"] == 0

    def turn(route, work, build, versions=False):
        phase = {"quick": "quickMs", "agent": "agentMs"}[route]
        return {"kind": "turn", "route": route, "versions": versions, "status": "applied",
                "phases": {phase: work}, "builds": [{"attempt": 1, "ms": build, "ok": True}]}

    for entry in [turn("quick", 3000, 500), turn("quick", 2000, 700), turn("quick", 4000, 600),
                  turn("agent", 9000, 800), turn("quick", 9999, 600, versions=True)]:
        record(tmp_path, entry)

    quick = estimate_turn(tmp_path, "quick")
    assert quick["phases"] == [{"name": "writing", "ms": 3000}, {"name": "compiling", "ms": 600}]
    assert quick["samples"] == 3
    assert estimate_turn(tmp_path, "pick")["phases"] == [{"name": "compiling", "ms": 600}]
