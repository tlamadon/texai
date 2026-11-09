"""The document's own macros, so math in the agent's replies renders as in the paper.

Replies quote the paper's notation — ``$\\E[Y_t]$``, ``$\\R^n$`` — and a
renderer that does not know those macros shows red errors where the paper shows
symbols. So the preamble is read for the simple definitions KaTeX can take on
directly: ``\\newcommand`` and friends with plain mandatory arguments, ``\\def``
with ``#1``-style parameters, and ``\\DeclareMathOperator``. The local files
the preamble loads (a ``macros.sty``, an ``\\input{preamble}``) are read too,
as TeX would find them; packages from the TeX distribution are not.

Anything subtler — optional arguments, ``\\NewDocumentCommand``, conditionals —
is left out rather than half-translated: KaTeX then shows that macro as an
error, and the reply falls back to its raw LaTeX, which is what it showed
before. Nothing here runs TeX; it only reads text.
"""

from __future__ import annotations

import re
from pathlib import Path

from .build import _COMMENT, _LOADS, _SUFFIXES, _read
from .config import AppConfig

__all__ = ["document_macros"]

MAX_FILES = 20
MAX_MACROS = 500
MAX_BODY = 2000

_NEWCOMMAND = re.compile(r"\\(?:re)?(?:new|provide)command\*?\s*")
_DEF = re.compile(r"\\[gex]?def\s*(\\[A-Za-z@]+)\s*((?:#\d)*)\s*(?=\{)")
_OPERATOR = re.compile(r"\\DeclareMathOperator(\*?)\s*")
_NAME = re.compile(r"\{?\s*(\\[A-Za-z@]+|\\.)\s*\}?")


def _group(text: str, start: int) -> tuple[str, int] | None:
    """The balanced ``{...}`` group opening at ``start``: its inside, and the index after it."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2  # an escaped brace, or any control symbol, never counts
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return None


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _definitions(text: str) -> dict[str, str]:
    macros: dict[str, str] = {}

    for match in _NEWCOMMAND.finditer(text):
        name = _NAME.match(text, match.end())
        if not name:
            continue
        i = _skip_space(text, name.end())
        if i < len(text) and text[i] == "[":
            close = text.find("]", i)
            if close < 0 or not text[i + 1 : close].strip().isdigit():
                continue
            i = _skip_space(text, close + 1)
            if i < len(text) and text[i] == "[":
                continue  # an optional argument: not something KaTeX can take
        body = _group(text, i)
        if body is not None:
            macros[name.group(1)] = body[0]

    for match in _DEF.finditer(text):
        body = _group(text, match.end())
        if body is not None:
            macros[match.group(1)] = body[0]

    for match in _OPERATOR.finditer(text):
        name = _NAME.match(text, match.end())
        if not name:
            continue
        body = _group(text, _skip_space(text, name.end()))
        if body is not None:
            star = "*" if match.group(1) else ""
            macros[name.group(1)] = f"\\operatorname{star}{{{body[0]}}}"

    return macros


def document_macros(config: AppConfig) -> dict[str, str]:
    """``{"\\\\name": "body"}`` for every simple macro the preamble defines."""
    root = config.root_tex
    if root is None or not root.is_file():
        return {}

    macros: dict[str, str] = {}
    queue: list[tuple[Path, bool]] = [(root, True)]
    seen: set[Path] = set()
    while queue and len(seen) < MAX_FILES:
        path, is_root = queue.pop(0)
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        text = _COMMENT.sub("", _read(path))
        if is_root and "\\begin{document}" in text:
            text = text.split("\\begin{document}", 1)[0]

        for name, body in _definitions(text).items():
            if len(body) <= MAX_BODY and len(macros) < MAX_MACROS:
                macros[name] = body

        for kind, names in _LOADS.findall(text):
            for item in (n.strip() for n in names.split(",")):
                if not item:
                    continue
                candidate = root.parent / item
                if candidate.suffix != _SUFFIXES[kind]:
                    candidate = candidate.with_name(candidate.name + _SUFFIXES[kind])
                if candidate.is_file():
                    queue.append((candidate, False))
    return macros
