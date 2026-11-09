"""texai's own agent tools, served over MCP for agents that run out of process.

Claude takes ``show_in_pdf``, ``offer_options`` and ``read_paper`` as in-process
tools (see :func:`texai.agent.build_navigation_server`). Codex runs as a separate
process, so it reaches the same tools here instead: a streamable-HTTP MCP endpoint
mounted on texai's own server, on the loopback port it already listens on. The
handlers are the same (:func:`texai.agent.navigate_to`,
:func:`texai.options.offer_options`, :func:`texai.papers.read_paper`), so both
agents get the same answer back.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from .agent import SHOW_IN_PDF_DESCRIPTION, navigate_to
from .config import AppConfig
from .events import EventBus
from .options import OFFER_OPTIONS_DESCRIPTION, offer_options
from .papers import READ_PAPER_DESCRIPTION, read_paper
from .record_tools import (
    ADD_SOURCE_DESCRIPTION,
    ANNOTATIONS_DESCRIPTION,
    COMMENT_DESCRIPTION,
    add_source,
    annotations_for,
    comment,
)

__all__ = ["TOOLS_MOUNT", "TOOLS_PATH", "build_tools_server", "tools_url"]

# Where the endpoint lives: the sub-application is mounted at TOOLS_MOUNT and
# answers MCP on its own /mcp route beneath it.
TOOLS_MOUNT = "/agent-tools"
TOOLS_PATH = f"{TOOLS_MOUNT}/mcp"


class Option(BaseModel):
    """One version of the passage, as ``offer_options`` takes it."""

    label: str
    text: str


def tools_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{TOOLS_PATH}"


def build_tools_server(config: AppConfig, bus: EventBus) -> Any:
    """A FastMCP server carrying ``show_in_pdf``, ``offer_options`` and ``read_paper``.

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

    @server.tool(name="offer_options", description=OFFER_OPTIONS_DESCRIPTION)
    async def offer(file: str, original: str, options: list[Option], note: str = "") -> str:
        return await offer_options(
            config, bus, file, original, [option.model_dump() for option in options], note
        )

    @server.tool(name="read_paper", description=READ_PAPER_DESCRIPTION)
    async def paper(target: str, pages: str = "", find: str = "") -> str:
        return await asyncio.to_thread(read_paper, config, target, pages, find)

    @server.tool(name="annotations", description=ANNOTATIONS_DESCRIPTION)
    async def attached(file: str, line: int, to_line: int | None = None) -> str:
        return await asyncio.to_thread(annotations_for, config, file, line, to_line)

    @server.tool(name="add_source", description=ADD_SOURCE_DESCRIPTION)
    async def source(
        file: str, line: int, target: str, page: int | None = None, quote: str = "", note: str = ""
    ) -> str:
        return await asyncio.to_thread(add_source, config, file, line, target, page, quote, note)

    @server.tool(name="comment", description=COMMENT_DESCRIPTION)
    async def note(file: str, text: str, line: int = 0, reply_to: str = "") -> str:
        return await asyncio.to_thread(comment, config, file, line, text, reply_to)

    return server

