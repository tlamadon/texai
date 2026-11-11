"""The slides of a beamer deck, and the pieces of each, read from the source.

SyncTeX cannot see inside a slide. Beamer collects a frame's body before it
typesets it, so everything on the slide is recorded against the line of its
``\\end{frame}``, and a click anywhere on it comes back as that one line. What
the source can still say for itself is where each frame is and what it is made
of, so that is read here: the frame, and inside it the *blocks* a reader would
point at — its title, each ``\\item``, each paragraph of plain text::

    \\begin{frame}{Identification}          title   line 1
      We compare movers and stayers.        text    line 2

      \\begin{itemize}
        \\item<1-> Movers switch firms       item    line 5
        \\item<2-> Stayers do not            item    lines 6-9, with its sub-list
          \\begin{itemize}
            \\item which is the control      item    line 8
          \\end{itemize}
      \\end{itemize}
    \\end{frame}

Each block carries its lines, so pointing at one names an exact range of the
source, and the text it renders as (see :func:`texai.words.project`), which is
how the page finds where to draw it. Nothing here runs TeX; it only reads text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .words import project

__all__ = ["Block", "Frame", "frame_at", "frames_in"]

# How much of a block's text travels to the page: enough to find its first
# rendered line, and to tell it from its neighbours.
MAX_BLOCK_TEXT = 240

_COMMENT = re.compile(r"(?<!\\)%.*$")
_BEGIN_FRAME = re.compile(r"\\begin\s*\{frame\}")
_END_FRAME = re.compile(r"\\end\s*\{frame\}")
# \frame{...}, \frame<1>[plain]{...} — but not \frametitle or \framebreak.
_FRAME_COMMAND = re.compile(r"\\frame(?![A-Za-z])\s*(?:<[^>\n]*>)?\s*(?:\[[^\]\n]*\])?\s*\{")
_OVERLAY_AND_OPTIONS = re.compile(r"\s*(?:<[^>\n]*>)?\s*(?:\[[^\]\n]*\])?\s*")
_FRAME_TITLE = re.compile(r"\\frametitle\s*(?:<[^>\n]*>)?\s*(?:\[[^\]\n]*\])?\s*\{")
_STRUCTURE = re.compile(r"\\(begin|end)\s*\{([A-Za-z@*]+)\}|\\item(?![A-Za-z])")
_LETTERS = re.compile(r"[^\W\d_]{2,}")


@dataclass(frozen=True)
class Block:
    """One thing on a slide a reader would point at."""

    kind: str  # title | item | text
    start: int  # 1-based, inclusive
    end: int
    text: str  # roughly what it renders as
    level: int = 0  # for an item: how many items it sits inside

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "level": self.level,
        }


@dataclass(frozen=True)
class Frame:
    start: int  # the line of \begin{frame}, or of \frame{
    end: int  # the line of \end{frame}, or of the closing brace
    title: str = ""
    blocks: tuple[Block, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "title": self.title,
            "blocks": [block.as_dict() for block in self.blocks],
        }


def _code(line: str) -> str:
    return _COMMENT.sub("", line)


def _group(text: str, start: int) -> tuple[str, int] | None:
    """The balanced ``{...}`` opening at ``start``: its inside, and the index after it."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    position = start
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2  # an escaped brace is not a brace
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : position], position + 1
        position += 1
    return None


def _rendered(lines: list[str], first: int, last: int) -> str:
    """What lines ``first``-``last`` render as, run together and cut to length."""
    parts = []
    for number in range(first, last + 1):
        text = " ".join(project(lines[number - 1])[0].split())
        if text:
            parts.append(text)
        if sum(len(part) + 1 for part in parts) >= MAX_BLOCK_TEXT:
            break
    return " ".join(parts)[:MAX_BLOCK_TEXT].strip()


def _has_words(text: str) -> bool:
    return bool(_LETTERS.search(text))


def _close_brace_line(lines: list[str], line: int, column: int) -> int | None:
    """The line holding the brace that closes the one at ``line``/``column``."""
    depth = 0
    for number in range(line, len(lines) + 1):
        code = _code(lines[number - 1])
        position = column if number == line else 0
        while position < len(code):
            char = code[position]
            if char == "\\":
                position += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return number
            position += 1
    return None


@dataclass
class _Open:
    """An environment still open while a frame's body is read."""

    name: str
    line: int
    item: int | None = None  # where its current \item began
    text_stop: int | None = None  # where that item's own words end
    had_items: bool = False


@dataclass
class _Body:
    blocks: list[Block] = field(default_factory=list)
    covered: set[int] = field(default_factory=set)


def _trim(lines: list[str], first: int, last: int) -> int:
    """``last`` pulled back over blank lines, never before ``first``."""
    while last > first and not _code(lines[last - 1]).strip():
        last -= 1
    return last


def _read_body(lines: list[str], first: int, last: int) -> _Body:
    """The items in lines ``first``-``last``, and the lines their lists cover."""
    body = _Body()
    stack: list[_Open] = []

    def close_item(env: _Open, before: int, alone: bool) -> None:
        if env.item is None:
            return
        end = before - 1 if alone and before > env.item else before
        end = _trim(lines, env.item, max(env.item, end))
        stop = env.text_stop if env.text_stop is not None else end + 1
        level = sum(1 for other in stack if other is not env and other.item is not None)
        body.blocks.append(
            Block(
                kind="item",
                start=env.item,
                end=end,
                text=_rendered(lines, env.item, max(env.item, min(end, stop - 1))),
                level=level,
            )
        )
        env.item = None
        env.text_stop = None

    for number in range(first, last + 1):
        code = _code(lines[number - 1])
        for found in _STRUCTURE.finditer(code):
            alone = not code[: found.start()].strip()
            if found.group(1) == "begin":
                if stack and stack[-1].item is not None and stack[-1].text_stop is None:
                    stack[-1].text_stop = number
                stack.append(_Open(found.group(2), number))
            elif found.group(1) == "end":
                names = [env.name for env in stack]
                if found.group(2) not in names:
                    continue  # an \end with no \begin in this frame
                while stack:
                    env = stack.pop()
                    close_item(env, number, alone)
                    if env.had_items and not any(other.had_items for other in stack):
                        body.covered.update(range(env.line, number + 1))
                    if env.name == found.group(2):
                        break
            else:  # \item
                if not stack:
                    # A list that is not an environment here: opened by a macro.
                    stack.append(_Open("", number))
                env = stack[-1]
                close_item(env, number, alone)
                env.item = number
                env.had_items = True

    while stack:  # whatever the frame closed for them
        env = stack.pop()
        close_item(env, last + 1, True)
        if env.had_items:
            body.covered.update(range(env.line, last + 1))
    return body


def _whole(lines: list[str], start: int, end: int, low: int, high: int) -> tuple[int, int]:
    """Lines ``start``-``end`` widened until every environment they open or
    close is whole, without leaving ``low``-``high``.

    A paragraph starting ``\\begin{block}{Main point}`` runs on to its
    ``\\end{block}``; one ending in an ``\\end`` reaches back to its ``\\begin``.
    """
    open_ = 0  # \begins inside the range still waiting for their \end
    orphans = 0  # \ends inside the range whose \begin is above it
    for number in range(start, end + 1):
        for found in _STRUCTURE.finditer(_code(lines[number - 1])):
            if found.group(1) == "begin":
                open_ += 1
            elif found.group(1) == "end":
                if open_:
                    open_ -= 1
                else:
                    orphans += 1

    number = end
    while open_ and number < high:
        number += 1
        for found in _STRUCTURE.finditer(_code(lines[number - 1])):
            if found.group(1) == "begin":
                open_ += 1
            elif found.group(1) == "end" and open_:
                open_ -= 1
                if not open_:
                    break
    if not open_:
        end = number

    number = start
    while orphans and number > low:
        number -= 1
        for found in reversed(list(_STRUCTURE.finditer(_code(lines[number - 1])))):
            if found.group(1) == "end":
                orphans += 1
            elif found.group(1) == "begin" and orphans:
                orphans -= 1
                if not orphans:
                    break
    if not orphans:
        start = number
    return start, end


def _text_blocks(lines: list[str], first: int, last: int, covered: set[int]) -> list[Block]:
    """Blank-line-separated runs of prose outside the lists."""
    blocks: list[Block] = []
    run: list[int] = []

    def flush() -> None:
        if run:
            # A paragraph that opens an environment is that environment: its
            # lines are what gets edited, and half a \begin{block} is no edit.
            start, end = _whole(lines, run[0], run[-1], first, last)
            if not any(block.start == start and block.end == end for block in blocks):
                blocks.append(
                    Block(kind="text", start=start, end=end, text=_rendered(lines, start, end))
                )
        run.clear()

    for number in range(first, last + 1):
        # A line with no words of its own ends a paragraph as a blank one does:
        # the \column between two columns' text, a \vfill.
        if number in covered or not _has_words(project(lines[number - 1])[0]):
            flush()
        else:
            run.append(number)
    flush()
    return blocks


def _blocks(lines: list[str], start: int, end: int, title: Block | None) -> tuple[Block, ...]:
    first, last = start + 1, end - 1
    blocks: list[Block] = []
    covered: set[int] = set()

    if title is None:
        for number in range(first, last + 1):
            found = _FRAME_TITLE.search(_code(lines[number - 1]))
            group = _group(_code(lines[number - 1]), found.end() - 1) if found else None
            if group is not None:
                text = " ".join(project(group[0])[0].split())
                title = Block(kind="title", start=number, end=number, text=text[:MAX_BLOCK_TEXT])
                covered.add(number)
                break
    if title is not None and title.text:
        blocks.append(title)

    if first <= last:
        body = _read_body(lines, first, last)
        blocks.extend(body.blocks)
        blocks.extend(_text_blocks(lines, first, last, covered | body.covered))

    return tuple(sorted(blocks, key=lambda b: (b.kind != "title", b.start, b.end)))


def frames_in(lines: list[str]) -> list[Frame]:
    """Every frame in a file, top to bottom, with its blocks.

    ``lines`` is the file split on newlines. A frame left open at the end of
    the file is not one: half-typed source should not move every icon.
    """
    frames: list[Frame] = []
    number = 1
    total = len(lines)
    while number <= total:
        code = _code(lines[number - 1])

        begin = _BEGIN_FRAME.search(code)
        if begin:
            end = None
            for candidate in range(number, total + 1):
                rest = _code(lines[candidate - 1])
                found = _END_FRAME.search(rest, begin.end() if candidate == number else 0)
                if found:
                    end = candidate
                    break
            if end is None:
                break
            after = _OVERLAY_AND_OPTIONS.match(code, begin.end())
            group = _group(code, after.end() if after else begin.end())
            title = None
            if group is not None:
                text = " ".join(project(group[0])[0].split())
                title = Block(kind="title", start=number, end=number, text=text[:MAX_BLOCK_TEXT])
            blocks = _blocks(lines, number, end, title)
            named = next((block.text for block in blocks if block.kind == "title"), "")
            frames.append(Frame(start=number, end=end, title=named, blocks=blocks))
            number = end + 1
            continue

        command = _FRAME_COMMAND.search(code)
        if command:
            end = _close_brace_line(lines, number, command.end() - 1)
            if end is None:
                break
            blocks = _blocks(lines, number, end, None) if end > number else ()
            named = next((block.text for block in blocks if block.kind == "title"), "")
            frames.append(Frame(start=number, end=end, title=named, blocks=blocks))
            number = end + 1
            continue

        number += 1
    return frames


def frame_at(lines: list[str], line: int) -> Frame | None:
    """The frame holding ``line`` — its ``\\end{frame}`` included, which is the
    line SyncTeX gives for everything on the slide."""
    for frame in frames_in(lines):
        if frame.start <= line <= frame.end:
            return frame
    return None
