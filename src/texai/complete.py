"""Fill-in-the-middle completions for the editor's ghost text.

Deliberately separate from :mod:`texai.agent`. That session is a long-lived,
tool-using conversation that *edits* the document across turns; this is a
single, stateless, low-latency call that only *reads* the surrounding text and
answers with the few characters to insert at the cursor. Different latency,
different semantics, different model — so a different door.

The agent reaches Claude through the ``claude`` CLI; completions go straight to
the Anthropic API with the plain SDK, so they need their own credentials
(``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` in the environment, or an
``ant auth login`` profile on disk). When neither is present the feature reports
itself disabled and the editor hides it, exactly as the chat panel does when the
agent SDK is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["CompletionUnavailable", "completion_status", "complete_text"]

# Haiku is the right tier for keystroke-adjacent latency: a short insertion does
# not need a frontier model, and the round trip is what the user feels.
MODEL = os.environ.get("TEXAI_COMPLETION_MODEL", "claude-haiku-4-5")

# How much of the buffer to send either side of the cursor. Local context is
# what a continuation actually turns on; sending the whole paper would only add
# latency and cost. Characters, not tokens — a cheap, predictable bound.
PREFIX_WINDOW = 6000
SUFFIX_WINDOW = 2000

# Short by design: ghost text is a nudge, not a paragraph. Keeps the call fast.
MAX_TOKENS = 64

# The editor waits on this; well under the SDK's ten-minute default so a slow or
# wedged request fails fast enough to retry on the next pause instead of hanging.
TIMEOUT_S = 8.0

CURSOR = "<CURSOR/>"

SYSTEM_PROMPT = (
    "You are an inline autocomplete for LaTeX source files, like a code editor's "
    f"ghost text. The document contains a {CURSOR} marker at the caret. Output only "
    "the text to insert there so the writing continues naturally.\n\n"
    "Rules:\n"
    "- Continue from exactly the characters immediately before the caret. Match "
    "spacing precisely: if the caret follows a space, do not start with another "
    "space; if it follows a partial word, finish that word before continuing.\n"
    "- Return a clean, grammatical continuation — finish the current phrase or "
    "sentence. Never start with a stray fragment or an unrelated word.\n"
    "- Keep it short: at most one sentence or clause.\n"
    "- Match the surrounding LaTeX conventions, macros, and notation.\n"
    "- Output only the raw insertion — no explanation, no markdown code fences, no "
    "repetition of the surrounding text.\n"
    "- If no natural continuation fits, output nothing."
)

DISABLED_HINT = (
    "Autocomplete is off by default. Launch texai with TEXAI_AUTOCOMPLETE=1 to "
    "turn it on."
)
INSTALL_HINT = (
    "The `anthropic` package is missing — run `uv sync` (or "
    "`pip install --upgrade texai`) and restart."
)
AUTH_HINT = (
    "No Anthropic API credentials found. Autocomplete calls the API directly, so "
    "set ANTHROPIC_API_KEY (or run `ant auth login`). This is separate from the "
    "`claude` CLI the agent uses."
)


class CompletionUnavailable(RuntimeError):
    """Completions cannot run (SDK missing, no credentials, or the call failed)."""


def _has_credentials() -> bool:
    """Whether the anthropic SDK will find credentials without a network call.

    Mirrors the SDK's resolution order closely enough to gate the UI: an API key
    or auth token in the environment, or an ``ant auth login`` profile on disk.
    A wrong or expired credential still gets past this and surfaces as a plain
    error on the first call — this only spares the UI from offering a feature
    that has no chance of working.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".config" / "anthropic"
    credentials = base / "credentials"
    return credentials.is_dir() and any(credentials.iterdir())


def _enabled() -> bool:
    """Whether the operator opted the feature in for this run.

    Off by default: the suggestions did not earn their place yet, so the whole
    feature stays dark — no menu item, no requests — unless explicitly asked for.
    The code all remains; this is the one switch that wakes it.
    """
    return os.environ.get("TEXAI_AUTOCOMPLETE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def completion_status() -> tuple[bool, str | None]:
    """Whether completions can run here, and why not if they cannot."""
    if not _enabled():
        return False, DISABLED_HINT
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, INSTALL_HINT
    if not _has_credentials():
        return False, AUTH_HINT
    return True, None


_client = None


def _get_client():
    """One AsyncAnthropic client, reused: each completion is a fresh keystroke-
    triggered request, but the connection pool underneath is worth keeping."""
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic()
    return _client


def build_window(text: str, offset: int) -> tuple[str, str]:
    """The bounded prefix/suffix around the cursor that gets sent to the model."""
    offset = max(0, min(len(text), offset))
    prefix = text[max(0, offset - PREFIX_WINDOW) : offset]
    suffix = text[offset : offset + SUFFIX_WINDOW]
    return prefix, suffix


def clean(raw: str) -> str:
    """Strip the wrappers a model sometimes adds despite being told not to.

    Only fences and trailing blank lines come off — leading whitespace is left
    alone because a completion that starts with a space (mid-word, or after a
    macro) needs it.
    """
    text = raw.strip("\n")
    if text.startswith("```"):
        # Drop an opening fence line (``` or ```latex) and a closing fence.
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip("\n")
    return text.rstrip()


async def complete_text(file: str, text: str, offset: int) -> str:
    """The text to insert at ``offset`` in ``text``. Empty when nothing fits."""
    available, reason = completion_status()
    if not available:
        raise CompletionUnavailable(reason or "completions unavailable")

    prefix, suffix = build_window(text, offset)
    # Nothing before the cursor to continue from: nothing to suggest.
    if not prefix.strip():
        return ""

    client = _get_client().with_options(timeout=TIMEOUT_S)
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Filename: {file}\n\n{prefix}{CURSOR}{suffix}",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        raise CompletionUnavailable(str(exc)) from exc

    parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    return clean("".join(parts))
