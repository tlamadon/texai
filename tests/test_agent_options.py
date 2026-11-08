"""What the Claude session is started with."""

from pathlib import Path

import claude_agent_sdk

from texai import agent as agent_module
from texai.agent import BUILTIN_TOOLS, AgentSession
from texai.config import AppConfig
from texai.events import EventBus


async def test_the_session_sees_only_texais_tools(tmp_path: Path, monkeypatch):
    (tmp_path / "main.tex").write_text("\\begin{document}\nhi\n\\end{document}\n")
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    config = AppConfig.create(tmp_path, tmp_path / "main.pdf")
    seen = {}

    class FakeClient:
        def __init__(self, options):
            seen["options"] = options

        async def connect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(agent_module, "sdk_status", lambda: (True, None))

    await AgentSession(config, EventBus(), effort="medium").start()

    options = seen["options"]
    # The user's own MCP servers and connectors stay out, and so do the
    # built-ins texai does not use: each tool is carried in every request.
    assert options.strict_mcp_config is True
    assert list(options.mcp_servers) == ["texai"]
    assert options.tools == BUILTIN_TOOLS
    assert options.effort == "medium"
