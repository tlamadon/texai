"""Reading and writing project source files for the built-in editor.

Everything here is deliberately narrow: only text files a LaTeX build actually
reads, only inside the project root, and only with a hash of what the caller
believed it was editing. The agent may be rewriting the same file, so a blind
write would silently discard its work.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .config import AppConfig
from .paths import PathOutsideRootError, ensure_inside_root, to_project_relative
from .selection import atomic_write_text
from .snapshots import SOURCE_SUFFIXES, iter_source_files

__all__ = [
    "SourceError",
    "SourceConflict",
    "MAX_SOURCE_BYTES",
    "content_hash",
    "list_sources",
    "read_source",
    "write_source",
]

MAX_SOURCE_BYTES = 5 * 1024 * 1024


class SourceError(RuntimeError):
    """The file cannot be read or written (outside the root, wrong kind, too big)."""


class SourceConflict(SourceError):
    """The file changed underneath the editor since it was loaded."""


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
