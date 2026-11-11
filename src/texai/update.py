"""Is there a newer texai on PyPI?

The one request texai makes that is not to the agent: a GET of the package's
public metadata, carrying nothing about the paper or the machine beyond what
any HTTP request does. It runs off the event loop, is asked for by the viewer
rather than at start-up, is remembered for half a day, and fails silent — a
laptop on a train should not hear about it. ``--no-update-check`` switches it
off altogether.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import sys
import time
import urllib.request
from typing import Any, Callable

__all__ = ["UpdateCheck", "parse_version", "is_newer", "latest_on_pypi", "upgrade_command"]

PYPI_URL = "https://pypi.org/pypi/texai/json"
TIMEOUT = 4.0
FRESH_FOR = 12 * 3600  # an answer is good for half a day
RETRY_AFTER = 3600  # a failure is asked again after an hour


def parse_version(version: str) -> tuple[int, ...] | None:
    """``"0.2.18"`` as ``(0, 2, 18)``; None for anything that is not a plain release."""
    if not re.fullmatch(r"\d+(\.\d+)*", version.strip()):
        return None
    return tuple(int(part) for part in version.strip().split("."))


def is_newer(latest: str | None, current: str) -> bool:
    """Whether ``latest`` is a release after ``current``. Unsure means no."""
    theirs, ours = parse_version(latest or ""), parse_version(current)
    return theirs is not None and ours is not None and theirs > ours


def latest_on_pypi() -> str | None:
    """The newest release PyPI lists, or None when it cannot be asked."""
    request = urllib.request.Request(
        PYPI_URL, headers={"Accept": "application/json", "User-Agent": "texai-update-check"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=_tls()) as response:  # noqa: S310
        return json.load(response)["info"]["version"]


def _tls() -> ssl.SSLContext:
    """Certificates to check PyPI against.

    A Python that uv or Nix installed often has no system bundle to find, and
    every request would fail its handshake. certifi comes with the agent SDKs'
    HTTP client, so it is there in practice; without it, the system's will do.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - not installed, or its bundle is unreadable
        return ssl.create_default_context()


def upgrade_command(prefix: str | None = None) -> str:
    """How this install is brought up to date, guessed from where it lives."""
    where = (prefix if prefix is not None else sys.prefix).replace("\\", "/")
    if "/uv/tools/" in where:
        return "uv tool upgrade texai"
    if "/uv/" in where:  # uvx runs out of uv's cache
        return "uvx texai@latest"
    if "/pipx/" in where:
        return "pipx upgrade texai"
    return "pip install -U texai"


class UpdateCheck:
    """The newest release, asked for at most every half day."""

    def __init__(
        self,
        current: str,
        fetch: Callable[[], str | None] = latest_on_pypi,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.current = current
        self._fetch = fetch
        self._clock = clock
        self._latest: str | None = None
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            if self._clock() >= self._next:
                try:
                    self._latest = await asyncio.to_thread(self._fetch)
                    self._next = self._clock() + FRESH_FOR
                except Exception:  # noqa: BLE001 - offline, blocked, PyPI down: say nothing
                    self._next = self._clock() + RETRY_AFTER
        return {
            "current": self.current,
            "latest": self._latest,
            "available": is_newer(self._latest, self.current),
            "command": upgrade_command(),
        }
