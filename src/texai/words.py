"""Finding the exact word a click landed on, in the source.

SyncTeX resolves a point to a line and nothing finer — it reports ``Column:-1``
for every engine in practice. But the browser knows which word was under the
cursor, because PDF.js renders a text layer over the page. Matching that word
back into the source gets the rest of the way.

The gap to bridge is that a ``.tex`` line is not what the reader sees:
``the \\emph{elasticity} of substitution`` renders as ``the elasticity of
substitution``, ``caf\\'e`` renders as ``café``, and ``---`` renders as ``—``. So
each source line is *projected* into roughly what it renders as, carrying an
index back to the original column for every character it keeps. The match runs
on the projection; the column comes back through the index.

It also has to know when it has lost. A wrong line is worse than no column at
all, so a tie across different lines refuses rather than guesses.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

__all__ = [
    "WordHit",
    "Located",
    "locate",
    "locate_word",
    "normalize_rendered",
    "project",
    "MAX_WINDOW",
]

# How far from the line SyncTeX reported to look. A wrapped paragraph renders
# across several lines and SyncTeX names the line a box *started* on, so the
# word itself can sit a line or two later in the source.
MAX_WINDOW = 3

MIN_WORD = 2
MAX_WORD = 200
CONTEXT_REACH = 48  # characters either side that count as "nearby"

# What a PDF text layer hands back, versus what a .tex file holds.
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}
_PUNCTUATION = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-",
    "\u00a0": " ", "\u2009": " ", "\u2002": " ", "\u2003": " ", "\u202f": " ",
}

# Accent macros: \'e, \"u, \^o, \`a, \~n, \=a, \.z, \u{a}, \v{s}, \c{c}, \H{o}.
_ACCENT = re.compile(r"\\(?:['`^\"~=.]|[Hbcdkruv](?![A-Za-z]))\s*\{?\s*([A-Za-z])\}?")
# Any other control sequence: \emph, \textbf, \LaTeX, \\ — no visible text of
# its own that we can match against.
_CONTROL = re.compile(r"\\(?:[A-Za-z@]+\*?|[^A-Za-z])")
_COMMENT = re.compile(r"(?<!\\)%")


@dataclass(frozen=True)
class WordHit:
    """Where a rendered word sits in the source."""

    line: int
    column: int  # 1-based, into the raw source line
    text: str  # what the source actually holds there
    exact: bool  # matched literally, rather than through accent folding

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "column": self.column, "text": self.text, "exact": self.exact}


def normalize_rendered(text: str) -> str:
    """Fold a rendered string toward what the source is likely to spell."""
    folded = unicodedata.normalize("NFC", text)
    for source, target in {**_LIGATURES, **_PUNCTUATION}.items():
        folded = folded.replace(source, target)
    return " ".join(folded.split())


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def project(raw: str) -> tuple[str, list[int]]:
    """Roughly what a source line renders as, plus each character's source column.

    ``text[i]`` came from ``raw[index[i]]``, so a match at ``i`` reports the
    column the user would put a cursor on. Markup that produces no text of its
    own is dropped; an accent macro collapses onto its letter.
    """
    text: list[str] = []
    index: list[int] = []
    position = 0
    limit = len(raw)

    while position < limit:
        char = raw[position]

        if char == "%" and _COMMENT.match(raw, position):
            break  # a comment renders as nothing at all

        if char == "\\":
            accent = _ACCENT.match(raw, position)
            if accent:
                text.append(accent.group(1))
                index.append(accent.start(1))
                position = accent.end()
                continue
            control = _CONTROL.match(raw, position)
            if control:
                # \& \% \_ \$ \# escape a character that does render.
                escaped = raw[position + 1 : position + 2]
                if escaped in {"&", "%", "_", "$", "#", "{", "}"}:
                    text.append(escaped)
                    index.append(position + 1)
                position = control.end()
                continue

        if char in "{}$":
            position += 1  # grouping and math delimiters render as nothing
            continue

        if char == "~":
            text.append(" ")
            index.append(position)
            position += 1
            continue

        if char == "-":  # -- and --- render as single dashes
            start = position
            while position < limit and raw[position] == "-":
                position += 1
            text.append("-")
            index.append(start)
            continue

        if raw.startswith("``", position) or raw.startswith("''", position):
            text.append('"')
            index.append(position)
            position += 2
            continue

        text.append(char)
        index.append(position)
        position += 1

    return "".join(text), index


def _is_boundary(text: str, start: int, end: int) -> bool:
    """A match must not sit inside a longer word."""
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _occurrences(haystack: str, needle: str) -> list[int]:
    found: list[int] = []
    start = haystack.find(needle)
    while start != -1:
        if _is_boundary(haystack, start, start + len(needle)):
            found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def _side_bonus(haystack: str, start: int, end: int, before: list[str], after: list[str]) -> float:
    """How well the words around this occurrence match the words around the click.

    Directional on purpose: it is what tells the third "the" on a line from the
    first, which plain proximity cannot.
    """
    left = haystack[max(0, start - CONTEXT_REACH) : start].lower()
    right = haystack[end : end + CONTEXT_REACH].lower()

    score = 0.0
    for words, side in ((before, left), (after, right)):
        usable = [w for w in words if len(w) >= MIN_WORD]
        if not usable:
            continue
        hits = sum(1 for w in usable if w.lower() in side)
        score += 30.0 * hits / len(usable)
    return score


@dataclass(frozen=True)
class Located:
    """The answer, and why it is the answer — the reason is shown to the user."""

    hit: WordHit | None
    reason: str  # matched | no_word | not_found | ambiguous

    def explain(self, phrase: str, file: str, line: int) -> str:
        if self.reason == "matched":
            return ""
        if self.reason == "no_word":
            return "the click was not on any text"
        if self.reason == "ambiguous":
            return f"“{phrase}” appears in more than one likely spot near {file}:{line}"
        return f"could not find “{phrase}” in {file} near line {line}"


def locate_word(
    lines: list[str],
    line: int,
    phrase: str,
    before: list[str] | None = None,
    after: list[str] | None = None,
    window: int = MAX_WINDOW,
) -> WordHit | None:
    """Find ``phrase`` near ``line``, or return ``None`` rather than guess."""
    return locate(lines, line, phrase, before, after, window).hit


def locate(
    lines: list[str],
    line: int,
    phrase: str,
    before: list[str] | None = None,
    after: list[str] | None = None,
    window: int = MAX_WINDOW,
) -> Located:
    """As :func:`locate_word`, but says why when it comes back empty.

    ``lines`` is the whole source file and ``line`` is 1-based, as SyncTeX
    reported it. ``before``/``after`` are the rendered words on either side of
    the click, used to tell repeats apart.
    """
    target = normalize_rendered(phrase or "")
    if not lines or len(target) < MIN_WORD or len(target) > MAX_WORD:
        return Located(None, "no_word")

    hit = _best(lines, line, target, before or [], after or [], window)
    if hit is not None:
        return Located(hit, "matched")

    if " " in target:
        # A phrase that wraps in the render is not one run of source characters;
        # its first word is the honest thing to look for, with the rest as context.
        head, *rest = target.split(" ")
        hit = _best(lines, line, head, before or [], rest[:3] + (after or []), window)
        if hit is not None:
            return Located(hit, "matched")
        target = head

    # Nothing nearby. Some text renders a long way from where it is written —
    # \maketitle, a section heading pulled into a running head, a float caption
    # that drifted pages from its source. Proximity is useless there, so fall
    # back to the whole file and accept only an answer that cannot be wrong:
    # exactly one occurrence in it.
    hit, several = _unique(lines, target)
    if hit is not None:
        return Located(hit, "matched")
    return Located(None, "ambiguous" if several else "not_found")


def _unique(lines: list[str], target: str) -> tuple[WordHit | None, bool]:
    folded_target = _strip_accents(target).lower()
    found: list[WordHit] = []

    for number, raw in enumerate(lines, start=1):
        haystack, index = project(raw)
        if not haystack:
            continue

        positions = [(start, True) for start in _occurrences(haystack, target)]
        if not positions:
            folded = _strip_accents(haystack).lower()
            if len(folded) == len(haystack):
                positions = [(start, False) for start in _occurrences(folded, folded_target)]

        for start, exact in positions:
            end = start + len(target)
            column = index[start]
            last = index[min(end, len(index)) - 1]
            found.append(
                WordHit(line=number, column=column + 1, text=raw[column : last + 1], exact=exact)
            )
            if len(found) > 1:
                # More than one candidate and no proximity to choose between
                # them; saying nothing beats pointing at the wrong one.
                return None, True

    return (found[0], False) if found else (None, False)


def _best(
    lines: list[str],
    line: int,
    target: str,
    before: list[str],
    after: list[str],
    window: int,
) -> WordHit | None:
    folded_target = _strip_accents(target).lower()
    candidates: list[tuple[float, int, WordHit]] = []

    low = max(1, line - window)
    high = min(len(lines), line + window)
    for number in range(low, high + 1):
        raw = lines[number - 1]
        haystack, index = project(raw)
        if not haystack:
            continue
        distance = abs(number - line)

        positions = [(start, True) for start in _occurrences(haystack, target)]
        if not positions:
            folded = _strip_accents(haystack).lower()
            if len(folded) == len(haystack):  # folding kept the columns aligned
                positions = [(start, False) for start in _occurrences(folded, folded_target)]

        for start, exact in positions:
            end = start + len(target)
            score = 100.0 - 8.0 * distance + (10.0 if exact else 0.0)
            score += _side_bonus(haystack, start, end, before, after)
            column = index[start]
            last = index[min(end, len(index)) - 1]
            candidates.append(
                (
                    score,
                    number,
                    WordHit(
                        line=number,
                        column=column + 1,
                        text=raw[column : last + 1],
                        exact=exact,
                    ),
                )
            )

    if not candidates:
        return None

    candidates.sort(key=lambda c: (-c[0], c[2].line, c[2].column))
    best_score, _, best_hit = candidates[0]

    # Equally good matches on *different* lines mean the sentence itself is in
    # doubt; the line SyncTeX gave is then the more honest answer. A tie inside
    # one line is harmless by comparison — the line is right either way — so the
    # leftmost occurrence wins.
    tied_lines = {hit.line for score, _, hit in candidates if score == best_score}
    if len(tied_lines) > 1:
        return None
    return best_hit
