"""Versions of a passage the agent offers instead of choosing one itself.

Some requests have no single right answer — "tighten this", "a better title" —
and the user ends up asking for alternatives and then for one of them. Here the
agent can hand the alternatives over directly: ``offer_options`` names the exact
source text and two to four replacements for it, and the user picks one on the
page. The pick is applied by texai itself, as a plain text replacement, so it
lands at once and needs no second agent call.

The tool checks the offer against the file before accepting it, so a quote that
does not match is the agent's to fix while it is still working, not the user's
to discover when they click. The accepted offer goes out on the event bus; the
turn controller collects it from there when the agent's turn ends, as both
Claude (in-process) and Codex (over MCP) reach the same handler.

An offer lives on its turn and costs nothing to make: the file is untouched
until a version is picked. When the user would rather decide later, they keep
it as a proposal: :func:`propose` writes it into the file as ``%@propose`` lines
above the passage (see :mod:`texai.proposals`), widened to whole lines. That
copy outlasts the session — the margin shows it after a restart, git carries it
to a co-author — and picking from it later is the same as picking now.
"""

from __future__ import annotations

from typing import Any

from .config import AppConfig
from .events import EventBus
from .proposals import ProposalError, apply_proposal, find_proposal, insert_proposal
from .source import SourceError, read_source
from .selection import atomic_write_text

__all__ = [
    "MAX_OPTIONS",
    "MIN_OPTIONS",
    "OFFER_OPTIONS_DESCRIPTION",
    "OFFER_OPTIONS_SCHEMA",
    "OfferError",
    "OfferStale",
    "apply_option",
    "check_offer",
    "offer_options",
    "propose",
]

MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_LABEL = 60

OFFER_OPTIONS_DESCRIPTION = """
Offer the user several versions of one passage to choose from, instead of
editing it yourself. They see the versions side by side on the page and click
one; texai puts it in place.

`original` is the exact source text being replaced, copied character for
character from the file (Read it first), long enough to occur only once. Each
option's `text` is the complete replacement for `original`, and its `label` says
in a few words how it differs ("shorter", "active voice", "keeps the citation").
Give 2 to 4 options that are genuinely different. Do not also edit the passage:
the user's pick is the edit.
""".strip()

OFFER_OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "Source file, relative to the project root"},
        "original": {"type": "string", "description": "Exact source text the options replace"},
        "options": {
            "type": "array",
            "minItems": MIN_OPTIONS,
            "maxItems": MAX_OPTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["label", "text"],
            },
        },
        "note": {"type": "string", "description": "Optional one-line remark for the user"},
    },
    "required": ["file", "original", "options"],
}


class OfferError(ValueError):
    """The offer does not hold up against the file; the message says why."""


class OfferStale(OfferError):
    """The passage an offer was made for is no longer in the file as it was."""


def _occurrences(text: str, original: str) -> int:
    return text.count(original) if original else 0


def check_offer(config: AppConfig, file: Any, original: Any, options: Any, note: Any = "") -> dict[str, Any]:
    """The offer as the UI shows it, or :class:`OfferError` saying what to fix."""
    file = str(file or "").strip()
    original = str(original or "")
    try:
        source = read_source(config, file)
    except SourceError as exc:
        raise OfferError(str(exc)) from exc

    count = _occurrences(source["text"], original)
    if count == 0:
        raise OfferError(
            f"`original` does not occur in {source['file']}. Copy it exactly from the "
            "file, markup, line breaks and all — not from the rendered PDF."
        )
    if count > 1:
        raise OfferError(
            f"`original` occurs {count} times in {source['file']}; include more of the "
            "surrounding text so it points at one place."
        )

    if not isinstance(options, list):
        raise OfferError("`options` must be a list of {label, text}.")
    cleaned: list[dict[str, str]] = []
    for option in options:
        if not isinstance(option, dict):
            raise OfferError("Each option must be an object with `label` and `text`.")
        text = str(option.get("text") or "")
        label = " ".join(str(option.get("label") or "").split())[:MAX_LABEL]
        if not text.strip() or text == original:
            continue
        cleaned.append({"label": label or f"Option {len(cleaned) + 1}", "text": text})
    if not MIN_OPTIONS <= len(cleaned) <= MAX_OPTIONS:
        raise OfferError(
            f"Offer between {MIN_OPTIONS} and {MAX_OPTIONS} options that differ from "
            f"the original (got {len(cleaned)})."
        )

    line = source["text"][: source["text"].index(original)].count("\n") + 1
    return {
        "file": source["file"],
        "line": line,
        "original": original,
        "options": cleaned,
        "note": " ".join(str(note or "").split()),
        "picked": None,
    }


def propose(config: AppConfig, offer: dict[str, Any]) -> dict[str, Any]:
    """Write a checked offer into its file as a ``%@propose`` choice.

    The passage is widened to the whole lines it touches, and each option with
    it, so the choice replaces lines. Returns the offer with the choice's ``id``
    and the lines it covers.
    """
    try:
        source = read_source(config, offer["file"])
    except SourceError as exc:
        raise OfferStale(str(exc)) from exc
    text = source["text"]
    if _occurrences(text, offer["original"]) != 1:
        raise OfferStale(f"the passage in {offer['file']} has changed since it was offered")
    at = text.index(offer["original"])
    head = text.rfind("\n", 0, at) + 1
    tail = text.find("\n", at + len(offer["original"]))
    tail = len(text) if tail == -1 else tail
    prefix = text[head:at]
    suffix = text[at + len(offer["original"]) : tail]
    start = text[:head].count("\n") + 1
    stop = start + text[head:tail].count("\n")
    options = [
        {"label": option["label"], "text": f"{prefix}{option['text']}{suffix}"}
        for option in offer["options"]
    ]
    new, proposal_id, moved = insert_proposal(text, start, stop, options)
    atomic_write_text(config.root / source["file"], new)
    return {
        **offer,
        "id": proposal_id,
        "line": moved + offer["line"] - start,
        "lines": text[head:tail],
    }


async def offer_options(
    config: AppConfig, bus: EventBus, file: Any, original: Any, options: Any, note: Any = ""
) -> str:
    """The body of ``offer_options`` for every agent; its answer, as text."""
    try:
        offer = check_offer(config, file, original, options, note)
    except OfferError as exc:
        return f"Not offered: {exc}"
    bus.publish("options_offered", offer=offer)
    return (
        f"Offered {len(offer['options'])} options for {offer['file']}:{offer['line']}. "
        "The user will pick one and texai will apply it — leave the passage as it is, "
        "and end your turn with a one-line note."
    )


def apply_option(config: AppConfig, offer: dict[str, Any], index: int) -> None:
    """Put option ``index`` in place of the offer's original text.

    An offer kept as a proposal is picked through its ``%@propose`` choice while
    that is still in the file, which also clears it. Otherwise — never kept,
    picked before, or the choice removed by hand — the text is swapped directly. If a version was picked before, it is what gets replaced:
    picking again is changing your mind. Raises :class:`OfferStale` when neither
    is in the file exactly once — edited since, by hand or by a later turn.
    """
    try:
        source = read_source(config, offer["file"])
    except SourceError as exc:
        raise OfferStale(str(exc)) from exc
    text = source["text"]
    if offer.get("id"):
        try:
            choice = find_proposal(text, offer["id"])
        except ProposalError:
            choice = None
        if choice is not None:
            if choice.original != offer.get("lines"):
                raise OfferStale(f"the passage in {offer['file']} has changed since it was offered")
            atomic_write_text(config.root / source["file"], apply_proposal(text, choice, index))
            return
    candidates = [offer["original"]]
    if offer.get("picked") is not None:
        candidates.insert(0, offer["options"][offer["picked"]]["text"])
    current = next((c for c in candidates if _occurrences(text, c) == 1), None)
    if current is None:
        raise OfferStale(f"the passage in {offer['file']} has changed since it was offered")
    replacement = offer["options"][index]["text"]
    atomic_write_text(config.root / source["file"], text.replace(current, replacement, 1))
