"""Turning what the agent says into a commit message."""

from __future__ import annotations

from pathlib import Path

import pytest

from texai import commitmsg
from texai.config import AppConfig
from texai.git import GitFile, GitStatus


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    (tmp_path / "main.pdf").write_bytes(b"%PDF")
    return AppConfig.create(tmp_path, tmp_path / "main.pdf")


@pytest.fixture()
def dirty() -> GitStatus:
    return GitStatus(repo=True, files=[GitFile("sections/model.tex", "modified", False)])


# ---------------------------------------------------------------- cleaning


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tighten the model section", "Tighten the model section"),
        ("  Tighten the model section  ", "Tighten the model section"),
        ('"Tighten the model section"', "Tighten the model section"),
        ("Tighten the model section.", "Tighten the model section"),
        ("```\nTighten the model section\n```", "Tighten the model section"),
        ("```text\nTighten the model section\n```", "Tighten the model section"),
        ("# Tighten the model section", "Tighten the model section"),
        ("- Tighten the model section", "Tighten the model section"),
        ("\n\nTighten the model section\n\n", "Tighten the model section"),
        ("Subject line\n\nA body that stays.", "Subject line\n\nA body that stays."),
        ("", ""),
        ("   \n  \n", ""),
    ],
)
def test_cleaning_strips_what_models_add(raw: str, expected: str):
    assert commitmsg._clean(raw) == expected


def test_cleaning_keeps_a_quoted_word_inside_a_subject():
    assert commitmsg._clean('Rename "labour" to "labor"') == 'Rename "labour" to "labor"'


def test_cleaning_keeps_the_body_intact():
    cleaned = commitmsg._clean("```\nSubject\n\nBody line one.\nBody line two.\n```")
    assert cleaned == "Subject\n\nBody line one.\nBody line two."


# ---------------------------------------------------------------- degrading


async def test_falls_back_when_there_is_no_diff(config: AppConfig, dirty: GitStatus):
    result = await commitmsg.propose_message(config, dirty, "")

    assert result["source"] == "fallback"
    assert result["message"] == "Update sections/model.tex"


async def test_falls_back_when_the_agent_is_unavailable(
    config: AppConfig, dirty: GitStatus, monkeypatch
):
    monkeypatch.setattr(commitmsg, "sdk_status", lambda: (False, "no agent here"))

    result = await commitmsg.propose_message(config, dirty, "a real diff")

    assert result["source"] == "fallback"
    assert result["reason"] == "no agent here"
    assert result["message"] == "Update sections/model.tex"


async def test_falls_back_when_the_agent_raises(config: AppConfig, dirty: GitStatus, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("the CLI went away")

    monkeypatch.setattr(commitmsg, "sdk_status", lambda: (True, None))
    monkeypatch.setattr(commitmsg, "_ask", explode)

    result = await commitmsg.propose_message(config, dirty, "a real diff")

    assert result["source"] == "fallback"
    assert "the CLI went away" in result["reason"]


async def test_falls_back_when_the_agent_says_nothing(
    config: AppConfig, dirty: GitStatus, monkeypatch
):
    async def empty(*args, **kwargs):
        return "   "

    monkeypatch.setattr(commitmsg, "sdk_status", lambda: (True, None))
    monkeypatch.setattr(commitmsg, "_ask", empty)

    result = await commitmsg.propose_message(config, dirty, "a real diff")
    assert result["source"] == "fallback"


async def test_falls_back_when_the_agent_takes_too_long(
    config: AppConfig, dirty: GitStatus, monkeypatch
):
    import asyncio

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return "never arrives"

    monkeypatch.setattr(commitmsg, "sdk_status", lambda: (True, None))
    monkeypatch.setattr(commitmsg, "_ask", slow)
    monkeypatch.setattr(commitmsg, "MESSAGE_TIMEOUT", 0.05)

    result = await commitmsg.propose_message(config, dirty, "a real diff")

    assert result["source"] == "fallback"
    assert "longer than" in result["reason"]


async def test_uses_what_the_agent_wrote(config: AppConfig, dirty: GitStatus, monkeypatch):
    async def wrote(*args, **kwargs):
        return "```\nTighten the identification argument.\n```"

    monkeypatch.setattr(commitmsg, "sdk_status", lambda: (True, None))
    monkeypatch.setattr(commitmsg, "_ask", wrote)

    result = await commitmsg.propose_message(config, dirty, "a real diff")

    assert result["source"] == "agent"
    assert result["message"] == "Tighten the identification argument"
