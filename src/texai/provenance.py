"""Provenance: what backs a sentence, written beside it in the source.

A ``%@src`` annotation (see :mod:`texai.annotations`) names where a claim comes
from. It sits on the line above the sentence it backs — one sentence per line
makes that the sentence itself — and the first word of it says what kind of
thing backs it::

    %@src #tab:main                                    this document: a \\label
    %@src code/estimate.py:120-145                     the project: a file, maybe lines
    %@src @card2018 p.14 "an elasticity of around 0.3" a paper: papers/<key>.pdf

After the target come optional details, in any order:

* ``p.14`` — a page of the PDF file (its own count, not the journal's);
* ``"..."`` — the passage it says, copied from the source. For a paper this is
  what finds the spot: page numbers drift between versions, words do not;
* ``sha:1a2b3c4d`` — the start of the SHA-256 of the PDF the quote was taken
  from. It finds the paper again if the file is renamed, and says so when the
  file in ``papers/`` is no longer the one quoted;
* ``-- free text`` — a note, for whoever reads it next.

Papers live in ``papers/`` at the project root, named after their bib key. The
bib entry, when one is found, only supplies a title for the peek.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote as url_quote

from .annotations import SOURCE, Annotation, parse_body
from .config import AppConfig
from .navigate import LocateError, locate
from .paths import PathOutsideRootError, ensure_inside_root, to_project_relative
from .snapshots import iter_source_files

__all__ = [
    "PAPERS_DIR",
    "SourceRef",
    "ProvenanceError",
    "file_sha",
    "parse_source",
    "resolve_source",
    "source_ref",
    "serve_path",
]

PAPERS_DIR = "papers"

# A peeked text file is shown whole, up to this much.
MAX_TEXT_BYTES = 512_000
# A short hash is plenty to tell the papers in one folder apart.
MIN_SHA = 6

_SHA = re.compile(r"^[0-9a-f]{%d,64}$" % MIN_SHA)
_LINES = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
# A comment runs from an unescaped % to the end of the line; a \label in one is
# not a label.
_COMMENT = re.compile(r"(?<!\\)%.*")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"})


class ProvenanceError(RuntimeError):
    """A source that cannot be looked at: outside the project, or malformed."""


@dataclass(frozen=True)
class SourceRef:
    """One ``%@src`` annotation, taken apart."""

    type: str  # "label", "file" or "paper"
    target: str  # the label, the path, or the bib key
    start: int | None = None  # lines in a file
    end: int | None = None
    page: int | None = None
    quote: str = ""
    sha: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_source(text: str) -> SourceRef | None:
    """Read the text after ``%@src``; ``None`` when there is no target."""
    return source_ref(parse_body(SOURCE, text.strip()))


def source_ref(parts: Mapping[str, Any] | Annotation) -> SourceRef | None:
    """A ``%@src`` taken apart by the shared grammar, read as a source."""
    get = parts.get if isinstance(parts, Mapping) else lambda name: getattr(parts, name)
    args = list(get("args"))
    if not args:
        return None
    head, *loose = args
    own = get("fields")
    page = int(own["p"]) if own.get("p", "").isdigit() else None
    sha = own.get("sha", "").lower()
    if not _SHA.match(sha):
        sha = ""
    # Words that are neither fields nor the target are read as part of the
    # note rather than thrown away: a person writing by hand should not lose
    # what they wrote.
    note = " ".join([*loose, get("text")]).strip()

    common = {"page": page, "quote": get("quote"), "sha": sha, "note": note}
    if head.startswith("#") and len(head) > 1:
        return SourceRef("label", head[1:], **common)
    if head.startswith("@") and len(head) > 1:
        return SourceRef("paper", head[1:], **common)

    start = end = None
    lines = _LINES.match(head)
    if lines is not None:
        head = lines.group("path")
        start = int(lines.group("start"))
        end = int(lines.group("end") or start)
        if end < start:
            start, end = end, start
    return SourceRef("file", head, start=start, end=end, **common)


# ------------------------------------------------------------------ hashing

_sha_cache: dict[Path, tuple[int, int, str]] = {}


def file_sha(path: Path) -> str:
    """The SHA-256 of a file, cached against its size and mtime."""
    stat = path.stat()
    cached = _sha_cache.get(path)
    if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
        return cached[2]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    sha = digest.hexdigest()
    _sha_cache[path] = (stat.st_size, stat.st_mtime_ns, sha)
    return sha


# ------------------------------------------------------------------ resolving


def serve_path(config: AppConfig, path: str) -> Path:
    """A project file that may be shown in the browser.

    Inside the root, and not under a hidden directory: ``.git`` and ``.texai``
    are the tools' business, and a ``.env`` has no reason to be peeked at.
    """
    try:
        resolved = ensure_inside_root(path, config.root)
    except PathOutsideRootError as exc:
        raise ProvenanceError(f"{path} is outside the project") from exc
    relative = resolved.relative_to(config.root)
    if any(part.startswith(".") for part in relative.parts):
        raise ProvenanceError(f"{path} is hidden")
    return resolved


def _file_url(relative: str) -> str:
    return f"/api/project-file?path={url_quote(relative)}"


def _find_label(config: AppConfig, label: str) -> tuple[str, int] | None:
    pattern = re.compile(r"\\label\s*\{\s*" + re.escape(label) + r"\s*\}")
    for path in iter_source_files(config.root):
        if path.suffix.lower() != ".tex":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.split("\n"), start=1):
            if pattern.search(_COMMENT.sub("", line)):
                return to_project_relative(path, config.root), number
    return None


def _locate_label(config: AppConfig, file: str, line: int) -> dict[str, Any] | None:
    """Where a ``\\label`` landed. The label itself typesets nothing, so the
    lines just above it — a caption, the equation it tags — stand in for it."""
    for candidate in (line, line - 1, line - 2, line - 3, line + 1, line + 2):
        if candidate < 1:
            continue
        try:
            answer = locate(config, file, candidate)
        except LocateError:
            return None
        if answer["found"]:
            return answer
    return None


def _resolve_label(config: AppConfig, ref: SourceRef) -> dict[str, Any]:
    found = _find_label(config, ref.target)
    if found is None:
        return {"view": "missing", "problem": f"No \\label{{{ref.target}}} in the source."}
    file, line = found
    answer: dict[str, Any] = {"view": "pdf", "url": "/api/pdf", "file": file, "line": line}
    spot = _locate_label(config, file, line)
    if spot is None:
        answer["problem"] = "Found the label, but not where it landed in the PDF; rebuild?"
    else:
        answer["page"] = spot["page"]
        answer["boxes"] = spot["boxes"]
    return answer


def _resolve_file(config: AppConfig, ref: SourceRef) -> dict[str, Any]:
    try:
        path = serve_path(config, ref.target)
    except ProvenanceError as exc:
        return {"view": "missing", "problem": str(exc)}
    if not path.is_file():
        return {"view": "missing", "problem": f"No file {ref.target} in the project."}
    relative = to_project_relative(path, config.root)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return {"view": "pdf", "url": _file_url(relative), "path": relative}
    if suffix in _IMAGE_SUFFIXES:
        return {"view": "image", "url": _file_url(relative), "path": relative}

    size = path.stat().st_size
    data = path.read_bytes()[:MAX_TEXT_BYTES]
    if b"\0" in data[:4096]:
        return {"view": "missing", "path": relative, "problem": f"{relative} is not a text file."}
    return {
        "view": "text",
        "path": relative,
        "text": data.decode("utf-8", errors="replace"),
        "truncated": size > MAX_TEXT_BYTES,
    }


def _resolve_paper(config: AppConfig, ref: SourceRef) -> dict[str, Any]:
    folder = config.root / PAPERS_DIR
    expected = folder / f"{ref.target}.pdf"
    answer: dict[str, Any] = {"bib": _bib_entry(config, ref.target)}

    path: Path | None = expected if expected.is_file() else None
    sha = file_sha(path) if path is not None else ""
    if ref.sha and not sha.startswith(ref.sha):
        # Not the file the quote was taken from, or no file by that name: the
        # hash may still find it under another one.
        moved = _find_by_sha(folder, ref.sha)
        if moved is not None:
            path, sha = moved, file_sha(moved)
            answer["moved"] = True
        elif path is not None:
            answer["problem"] = (
                f"{PAPERS_DIR}/{ref.target}.pdf has changed since this was written "
                f"(sha {sha[:8]}, expected {ref.sha[:8]}); the page or quote may be off."
            )
            answer["stale"] = True

    if path is None:
        answer.update(
            view="missing",
            path=f"{PAPERS_DIR}/{ref.target}.pdf",
            problem=f"No {PAPERS_DIR}/{ref.target}.pdf yet: put the paper there, named after its key.",
        )
        return answer
    relative = to_project_relative(path, config.root)
    answer.update(view="pdf", url=_file_url(relative), path=relative, sha=sha)
    return answer


def _find_by_sha(folder: Path, prefix: str) -> Path | None:
    if not folder.is_dir():
        return None
    for candidate in sorted(folder.glob("*.pdf")):
        try:
            if file_sha(candidate).startswith(prefix):
                return candidate
        except OSError:
            continue
    return None


def resolve_source(config: AppConfig, text: str) -> dict[str, Any]:
    """Everything the peek needs to show what a ``%@src`` points at.

    ``view`` says how: ``pdf`` (with a ``url``, and a ``page`` or ``boxes`` for
    where to look), ``text`` (the file's contents), ``image``, or ``missing``
    with a ``problem`` saying why. A ``problem`` can also come with a view, as a
    warning: a paper that has changed since it was quoted still opens.
    """
    ref = parse_source(text)
    if ref is None:
        raise ProvenanceError("This %@src names nothing: start it with #label, @key or a path.")
    resolver = {"label": _resolve_label, "file": _resolve_file, "paper": _resolve_paper}[ref.type]
    return {"source": ref.as_dict(), **resolver(config, ref)}


# ------------------------------------------------------------------ bibliography

_ENTRY = re.compile(r"@\s*(\w+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
_FIELD = re.compile(r"(\w+)\s*=\s*", re.IGNORECASE)


def _bib_entry(config: AppConfig, key: str) -> dict[str, str] | None:
    """Title, author and year for a key, from whichever ``.bib`` has it."""
    for path in iter_source_files(config.root):
        if path.suffix.lower() != ".bib":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for entry in _ENTRY.finditer(text):
            if entry.group(2) == key:
                return _bib_fields(text, entry.end())
    return None


def _bib_fields(text: str, at: int) -> dict[str, str]:
    """The fields of one entry, up to its closing brace. Values in braces or
    quotes, nested braces allowed; LaTeX markup is left as it is."""
    fields: dict[str, str] = {}
    depth = 1
    i = at
    while i < len(text) and depth > 0:
        match = _FIELD.match(text, i)
        if match is None:
            if text[i] == "}":
                depth -= 1
            i += 1
            continue
        name = match.group(1).lower()
        i = match.end()
        value, i = _bib_value(text, i)
        fields[name] = " ".join(value.replace("{", "").replace("}", "").split())
    return {k: fields[k] for k in ("title", "author", "year", "journal") if k in fields}


def _bib_value(text: str, i: int) -> tuple[str, int]:
    if i >= len(text):
        return "", i
    opener = text[i]
    if opener in "{\"":
        closer = "}" if opener == "{" else '"'
        depth = 0
        start = i + 1
        for j in range(i, len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if (closer == "}" and depth == 0 and j > i) or (
                closer == '"' and ch == '"' and j > i and depth == 0
            ):
                return text[start:j], j + 1
        return text[start:], len(text)
    end = i
    while end < len(text) and text[end] not in ",}\n":
        end += 1
    return text[i:end].strip(), end
