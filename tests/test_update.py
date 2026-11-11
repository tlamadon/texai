"""The update check: versions compared, PyPI asked rarely, failures silent."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai import __version__
from texai.server import create_app
from texai.config import AppConfig
from texai.update import FRESH_FOR, RETRY_AFTER, UpdateCheck, is_newer, upgrade_command


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = (tmp_path / "project").resolve()
    root.mkdir()
    (root / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return AppConfig.create(root, root / "main.pdf")


def test_only_a_later_plain_release_is_newer():
    assert is_newer("0.2.19", "0.2.18")
    assert is_newer("0.10.0", "0.9.9")  # numbers, not strings
    assert is_newer("1.0", "0.9.9")
    assert not is_newer("0.2.18", "0.2.18")
    assert not is_newer("0.2.17", "0.2.18")
    assert not is_newer(None, "0.2.18")
    assert not is_newer("0.3.0rc1", "0.2.18")  # unsure means no
    assert not is_newer("0.3.0", "0.2.18.dev0")


def test_the_upgrade_command_follows_the_install():
    assert upgrade_command("/home/a/.local/share/uv/tools/texai") == "uv tool upgrade texai"
    assert upgrade_command("/home/a/.cache/uv/archive-v0/abc") == "uvx texai@latest"
    assert upgrade_command("/home/a/.local/pipx/venvs/texai") == "pipx upgrade texai"
    assert upgrade_command("/home/a/paper/.venv") == "pip install -U texai"


async def test_pypi_is_asked_once_until_the_answer_is_stale():
    asked, now = [], [0.0]

    def fetch():
        asked.append(now[0])
        return "9.0.0"

    check = UpdateCheck("0.2.18", fetch=fetch, clock=lambda: now[0])
    first = await check.status()
    assert (first["available"], first["latest"], first["current"]) == (True, "9.0.0", "0.2.18")
    await check.status()
    assert len(asked) == 1
    now[0] = FRESH_FOR + 1
    await check.status()
    assert len(asked) == 2


async def test_a_failed_check_says_nothing_and_tries_again_later():
    now, calls = [0.0], []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("offline")
        return "9.0.0"

    check = UpdateCheck("0.2.18", fetch=fetch, clock=lambda: now[0])
    assert (await check.status())["available"] is False
    assert (await check.status())["available"] is False and len(calls) == 1
    now[0] = RETRY_AFTER + 1
    assert (await check.status())["available"] is True


def test_the_api_reports_it_and_asks_nothing_when_switched_off(config):
    off = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert off.get("/api/update").json() == {
        "current": __version__, "latest": None, "available": False, "command": None,
    }
    check = UpdateCheck("0.2.18", fetch=lambda: "0.3.0")
    on = TestClient(create_app(config, update_check=check), base_url="http://127.0.0.1")
    answer = on.get("/api/update").json()
    assert (answer["available"], answer["latest"]) == (True, "0.3.0") and answer["command"]
