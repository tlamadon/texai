"""texai's own agent tools, served over MCP for agents that run out of process.

Claude takes ``show_in_pdf`` as an in-process tool (see
:func:`texai.agent.build_navigation_server`). Codex runs as a separate process,
so it reaches the same tool here instead: a streamable-HTTP MCP endpoint mounted
on texai's own server, on the loopback port it already listens on. The handler
is the same :func:`texai.agent.navigate_to`, so both agents move the view the
same way and get the same answer back.
"""

from __future__ import annotations

from typing import Any

from .agent import SHOW_IN_PDF_DESCRIPTION, navigate_to
from .config import AppConfig
from .events import EventBus

__all__ = ["TOOLS_MOUNT", "TOOLS_PATH", "build_tools_server", "tools_url"]

# Where the endpoint lives: the sub-application is mounted at TOOLS_MOUNT and
# answers MCP on its own /mcp route beneath it.
TOOLS_MOUNT = "/agent-tools"
TOOLS_PATH = f"{TOOLS_MOUNT}/mcp"


def tools_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{TOOLS_PATH}"


def build_tools_server(config: AppConfig, bus: EventBus) -> Any:
    """A FastMCP server carrying ``show_in_pdf``.

    The caller mounts ``server.streamable_http_app()`` and must run
    ``server.session_manager.run()`` for the app's lifetime: a mounted
    sub-application's own lifespan is never started by the parent.
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # The SDK's own DNS-rebinding guard, held to the same loopback names texai's
    # middleware accepts everywhere else, with or without a port.
    names = ["127.0.0.1", "localhost", "[::1]"]
    security = TransportSecuritySettings(
        allowed_hosts=[*names, *(f"{name}:*" for name in names)],
        allowed_origins=[f"http://{name}:*" for name in names],
    )
    server = FastMCP(
        "texai",
        host="127.0.0.1",
        log_level="WARNING",
        transport_security=security,
        # One request, one JSON answer, no session to keep. The tool needs
        # neither streaming nor state, and an SSE reply races texai's HTTP
        # middleware: the request can close before the stream has started.
        json_response=True,
        stateless_http=True,
    )

    @server.tool(name="show_in_pdf", description=SHOW_IN_PDF_DESCRIPTION)
    async def show_in_pdf(file: str, line: int, why: str = "") -> str:
        return await navigate_to(config, bus, file, line, why)

    return server
