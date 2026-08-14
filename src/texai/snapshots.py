"""Per-turn source snapshots: the basis for both the diff view and revert.

Before the agent touches anything we copy every source file into
``.texai/snapshots/<turn-id>/``. That gives us a fixed "before" to diff
against, a way to undo a whole turn, and the old tree ``latexdiff`` needs.

Deliberately not git: the project may not be a repository, and if it is, we
should not be writing commits into the user's history behind their back.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .hunks import Hunk, file_hunks

__all__ = [
    "SOURCE_SUFFIXES",
    "Snapshot",
    "iter_source_files",
    "take_snapshot",
    "diff_against",
    "hunks_against",
    "read_pair",
    "restore",
]

# Text inputs a LaTeX build reads. Deliberately excludes images and build
# output — we snapshot what the agent might edit, not what latexmk regenerates.
SOURCE_SUFFIXES = frozenset({".tex", ".bib", ".cls", ".sty", ".bbx", ".cbx", ".lco"})

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 2000
IGNORED_DIRS = frozenset({".texai", ".git", ".svn", "node_modules", "__pycache__"})


@dataclass(frozen=True)
class Snapshot:
    """A copy of the project's source files at one point in time."""

    turn_id: str
    directory: Path
    files: frozenset[str]  # project-relative POSIX paths


def iter_source_files(root: Path) -> list[Path]:
    """Every snapshot-worthy source file under ``root``, sorted for determinism."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= MAX_FILES:
            break
        if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def take_snapshot(root: Path, snapshots_dir: Path, turn_id: str) -> Snapshot:
    """Copy the current source tree into a per-turn snapshot directory."""
    directory = snapshots_dir / turn_id
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    relatives: set[str] = set()
    for source in iter_source_files(root):
        relative = source.relative_to(root)
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        relatives.add(relative.as_posix())

    return Snapshot(turn_id=turn_id, directory=directory, files=frozenset(relatives))


def diff_against(snapshot: Snapshot, root: Path) -> list[dict[str, object]]:
    """Unified diff of every file that changed since ``snapshot`` was taken.

    Returns one entry per changed file with a ``status`` of ``modified``,
    ``added`` or ``deleted``, plus added/removed line counts.
    """
    current = {path.relative_to(root).as_posix() for path in iter_source_files(root)}
    changes: list[dict[str, object]] = []

    for relative in sorted(snapshot.files | current):
        before_path = snapshot.directory / relative
        after_path = root / relative
        before_exists = relative in snapshot.files and before_path.is_file()
        after_exists = relative in current and after_path.is_file()

        before = _read_text(before_path) if before_exists else ""
        after = _read_text(after_path) if after_exists else ""
        if before == after:
            continue

        if not before_exists:
            status = "added"
        elif not after_exists:
            status = "deleted"
        else:
            status = "modified"

        diff = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                n=3,
            )
        )
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        changes.append(
            {
                "file": relative,
                "status": status,
                "added": added,
                "removed": removed,
                "diff": "".join(diff),
            }
        )
    return changes


def restore(snapshot: Snapshot, root: Path) -> list[str]:
    """Put the source tree back the way it was; return the paths touched."""
    touched: list[str] = []

    for relative in sorted(snapshot.files):
        source = snapshot.directory / relative
        if not source.is_file():
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or _read_text(destination) != _read_text(source):
            shutil.copy2(source, destination)
            touched.append(relative)

    # Files the turn created did not exist in the snapshot; remove them.
    for path in iter_source_files(root):
        relative = path.relative_to(root).as_posix()
        if relative not in snapshot.files:
            path.unlink(missing_ok=True)
            touched.append(relative)

    return sorted(set(touched))


def read_pair(snapshot: Snapshot, root: Path, relative: str) -> tuple[str, str]:
    """The snapshot and current text of one file, either of which may be empty."""
    before_path = snapshot.directory / relative
    after_path = root / relative
    before = _read_text(before_path) if before_path.is_file() else ""
    after = _read_text(after_path) if after_path.is_file() else ""
    return before, after


def hunks_against(snapshot: Snapshot, root: Path) -> list[Hunk]:
    """Every changed region across every changed file, in file order."""
    current = {path.relative_to(root).as_posix() for path in iter_source_files(root)}
    found: list[Hunk] = []
    for relative in sorted(snapshot.files | current):
        before, after = read_pair(snapshot, root, relative)
        if before == after:
            continue
        found.extend(file_hunks(relative, before, after))
    return found
