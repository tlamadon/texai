"""Reading and writing project source files for the built-in editor.

Everything here is deliberately narrow: only text files a LaTeX build actually
reads, only inside the project root, and only with a hash of what the caller
believed it was editing. The agent may be rewriting the same file, so a blind
write would silently discard its work.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from .config import AppConfig
from .paragraphs import body_start_line, paragraph_ranges
from .paths import PathOutsideRootError, ensure_inside_root, to_project_relative
from .selection import atomic_write_text
from .snapshots import SOURCE_SUFFIXES, iter_source_files

__all__ = [
    "SourceError",
    "SourceConflict",
    "NoParagraph",
    "MAX_SOURCE_BYTES",
    "content_hash",
    "list_sources",
    "read_source",
    "write_source",
    "read_lines",
    "read_paragraph",
    "write_paragraph",
]

MAX_SOURCE_BYTES = 5 * 1024 * 1024


class SourceError(RuntimeError):
    """The file cannot be read or written (outside the root, wrong kind, too big)."""


class SourceConflict(SourceError):
    """The file changed underneath the editor since it was loaded."""


class NoParagraph(SourceError):
    """The line asked about is not part of a paragraph of body text."""


def content_hash(text: str) -> str:
    """Short digest used to detect edits landing on stale content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _resolve(config: AppConfig, file: str) -> Any:
    try:
        path = ensure_inside_root(file, config.root)
    except PathOutsideRootError as exc:
        raise SourceError(f"{file} is outside the project root") from exc
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        kinds = ", ".join(sorted(SOURCE_SUFFIXES))
        raise SourceError(f"{file} is not an editable source file (expected one of {kinds})")
    return path


def list_sources(config: AppConfig) -> list[str]:
    """Every editable source file in the project, as relative POSIX paths."""
    return [to_project_relative(path, config.root) for path in iter_source_files(config.root)]


def read_source(config: AppConfig, file: str) -> dict[str, Any]:
    path = _resolve(config, file)
    if not path.is_file():
        raise SourceError(f"No such file in the project: {file}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise SourceError(f"{file} is too large to edit here")

    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "file": to_project_relative(path, config.root),
        "text": text,
        "sha": content_hash(text),
        "lines": text.count("\n") + 1,
    }


def write_source(config: AppConfig, file: str, text: str, base_sha: str | None) -> dict[str, Any]:
    """Write the file, refusing if it moved under the editor.

    ``base_sha`` is the hash the editor loaded. A mismatch means the agent (or
    anything else) rewrote the file in the meantime, so the save is refused
    rather than clobbering it.
    """
    path = _resolve(config, file)
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise SourceError(f"{file} is too large to save")

    if path.is_file():
        current = path.read_text(encoding="utf-8", errors="replace")
        if base_sha and content_hash(current) != base_sha:
            raise SourceConflict(
                f"{file} changed since you opened it — reload before saving, "
                "or your edit would discard that change."
            )
    elif base_sha:
        raise SourceConflict(f"{file} no longer exists")

    atomic_write_text(path, text)
    return {
        "file": to_project_relative(path, config.root),
        "sha": content_hash(text),
        "lines": text.count("\n") + 1,
    }


# Only prose can be edited in place on the page: a .bib entry or a .sty macro
# shows up in the PDF too, but nowhere a paragraph-sized box would make sense.
_PARAGRAPH_SUFFIXES = frozenset({".tex"})


def read_lines(config: AppConfig, file: str, start: int, end: int) -> dict[str, Any]:
    """Lines ``start``-``end`` as they stand, in the shape of a paragraph.

    For a block the source names outright rather than by its blank lines: one
    ``\\item`` of a slide, a frame. Saved the same way (:func:`write_paragraph`),
    which checks the lines are still what was read.
    """
    data = read_source(config, file)
    if PurePosixPath(data["file"]).suffix.lower() not in _PARAGRAPH_SUFFIXES:
        raise NoParagraph(f"{data['file']}:{start} is not body text that can be edited in place")
    lines = data["text"].split("\n")
    if start < 1 or end < start or end > len(lines):
        raise NoParagraph(f"{data['file']} has no lines {start}-{end}")
    return {
        "file": data["file"],
        "index": None,
        "start": start,
        "end": end,
        "text": "\n".join(lines[start - 1 : end]),
        "sha": data["sha"],
    }


def read_paragraph(config: AppConfig, file: str, line: int) -> dict[str, Any]:
    """The blank-line-separated paragraph a source line belongs to.

    The same paragraphs the editor's gutter and the page's bars colour, so the
    box that opens on the page holds exactly the block wearing that colour.
    SyncTeX often names the blank line *after* a paragraph (where TeX read the
    ``\\par`` that ended it), so a blank line falls back to the paragraph just
    above it rather than refusing.
    """
    data = read_source(config, file)
    if PurePosixPath(data["file"]).suffix.lower() not in _PARAGRAPH_SUFFIXES:
        raise NoParagraph(f"{data['file']}:{line} is not body text that can be edited in place")

    text = data["text"]
    ranges = paragraph_ranges(text)
    found = next((r for r in ranges if r[1] <= line <= r[2]), None)
    if found is None:
        found = next((r for r in ranges if r[2] == line - 1), None)
    if found is None:
        raise NoParagraph(f"{data['file']}:{line} is not inside a paragraph")
    index, start, end = found

    # The box must never hold the document's own frame: a paragraph that runs
    # into \\begin{document} or \\end{document} is trimmed to the prose inside.
    lines = text.split("\n")
    body = body_start_line(text)
    if "\\begin{document}" in lines[body - 1]:
        start = max(start, body + 1)
    for number in range(start, end + 1):
        if "\\end{document}" in lines[number - 1]:
            end = number - 1
            break
    if start > end:
        raise NoParagraph(f"{data['file']}:{line} is not inside a paragraph of body text")
    return {
        "file": data["file"],
        "index": index,
        "start": start,
        "end": end,
        "text": "\n".join(lines[start - 1 : end]),
        "sha": data["sha"],
    }


def _find_block(lines: list[str], block: list[str], hint: int) -> int | None:
    """Where ``block`` sits in ``lines`` (0-based), preferring ``hint``.

    The agent may have inserted or removed lines above the paragraph while the
    box was open, which moves it without changing it. One unambiguous match
    elsewhere is still the paragraph; two are a guess, and a guess could
    overwrite the wrong one.
    """
    size = len(block)
    if lines[hint : hint + size] == block:
        return hint
    spots = [i for i in range(len(lines) - size + 1) if lines[i : i + size] == block]
    return spots[0] if len(spots) == 1 else None


def write_paragraph(
    config: AppConfig, file: str, start: int, end: int, original: str, text: str
) -> dict[str, Any]:
    """Replace one paragraph, refusing if it changed since it was read.

    Only the paragraph itself is compared, not the whole file: the agent editing
    some other part of the same file is no reason to throw this edit away.
    Blank lines at either end of the new text are dropped so the paragraph keeps
    the breaks around it; emptying it removes the paragraph and one of the
    breaks, rather than leaving a double gap.
    """
    path = _resolve(config, file)
    if path.suffix.lower() not in _PARAGRAPH_SUFFIXES:
        raise SourceError(f"{file} is not body text that can be edited in place")
    if not path.is_file():
        raise SourceConflict(f"{file} no longer exists")
    current = path.read_text(encoding="utf-8", errors="replace")
    lines = current.split("\n")

    block = original.split("\n")
    if start < 1 or end < start or end - start + 1 != len(block):
        raise SourceError("The paragraph's line range does not match its text")
    at = _find_block(lines, block, start - 1)
    if at is None:
        raise SourceConflict(
            f"That paragraph of {file} changed since you opened it — "
            "close the box and open it again to edit the new version."
        )

    replacement = text.replace("\r\n", "\n").split("\n")
    while replacement and not replacement[0].strip():
        replacement.pop(0)
    while replacement and not replacement[-1].strip():
        replacement.pop()

    relative = to_project_relative(path, config.root)
    if replacement == block:
        return {"file": relative, "changed": False, "start": at + 1, "end": at + len(block),
                "text": original, "sha": content_hash(current)}

    tail = at + len(block)
    if not replacement:
        # Take a neighbouring break with it, so two blank lines do not meet.
        if tail < len(lines) and not lines[tail].strip():
            tail += 1
        elif at > 0 and not lines[at - 1].strip():
            at -= 1
    updated = lines[:at] + replacement + lines[tail:]
    new_text = "\n".join(updated)
    if len(new_text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise SourceError(f"{file} is too large to save")

    atomic_write_text(path, new_text)
    return {
        "file": relative,
        "changed": True,
        "start": at + 1,
        "end": at + len(replacement),
        "text": "\n".join(replacement),
        "sha": content_hash(new_text),
    }
