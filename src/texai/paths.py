"""Path confinement helpers.

Every path that crosses the process boundary (CLI arguments, SyncTeX output) is
resolved to a real path and checked against the declared project root, so a
document that points somewhere else cannot make us read or advertise files
outside the project.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "PathOutsideRootError",
    "resolve_root",
    "ensure_inside_root",
    "resolve_user_path",
    "to_project_relative",
    "resolve_source_path",
]


class PathOutsideRootError(ValueError):
    """Raised when a resolved path escapes the declared project root."""

    def __init__(self, path: Path, root: Path) -> None:
        super().__init__(f"path {path} is outside the project root {root}")
        self.path = path
        self.root = root


def resolve_root(root: str | Path) -> Path:
    """Resolve the project root, following symlinks."""
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"project root is not a directory: {resolved}")
    return resolved


def ensure_inside_root(path: str | Path, root: Path) -> Path:
    """Resolve ``path`` and confirm it stays inside ``root``.

    ``Path.resolve`` collapses ``..`` segments and follows symlinks, so both
    ``root/../etc/passwd`` and a symlink pointing outside the tree are rejected.
    The path itself does not need to exist.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathOutsideRootError(resolved, root)
    return resolved


def resolve_user_path(value: str | Path, root: Path) -> Path:
    """Resolve a CLI path that may be relative to the CWD *or* to the root.

    Both readings are natural: ``--pdf build/main.pdf`` is usually meant
    relative to the project root, while a shell-completed ``--pdf
    ./example/main.pdf`` is relative to where the command was typed. Existing
    files win; otherwise the root-relative reading is used, and the result is
    always root-checked.
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return ensure_inside_root(path, root)

    escaped: PathOutsideRootError | None = None
    for base in (Path.cwd(), root):
        candidate = base / path
        try:
            resolved = ensure_inside_root(candidate, root)
        except PathOutsideRootError as exc:
            # Remember it only if the file is really there, so we can report
            # "outside the root" instead of "not found".
            if escaped is None and candidate.exists():
                escaped = exc
            continue
        if resolved.exists():
            return resolved

    fallback = root / path
    if escaped is not None and not fallback.exists():
        raise escaped
    return ensure_inside_root(fallback, root)


def to_project_relative(path: str | Path, root: Path) -> str:
    """Return ``path`` as a POSIX path relative to ``root``.

    Raises :class:`PathOutsideRootError` if the path escapes the root.
    """
    resolved = ensure_inside_root(path, root)
    return resolved.relative_to(root).as_posix()


def resolve_source_path(raw: str, root: Path, search_dirs: list[Path]) -> Path:
    """Turn a SyncTeX ``Input:`` value into an absolute path inside ``root``.

    SyncTeX may report an absolute path, or a path relative to the directory
    holding the ``.synctex.gz`` file, or relative to the compilation directory.
    Candidates are tried in order and the first existing one wins; if none
    exists we still return the first candidate so the caller can report a
    sensible location. The result is always root-checked.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("empty source path from synctex")

    candidates: list[Path] = []
    path = Path(cleaned)
    if path.is_absolute():
        candidates.append(path)
    else:
        for directory in search_dirs:
            candidates.append(directory / path)

    for candidate in candidates:
        if candidate.exists():
            return ensure_inside_root(candidate, root)
    return ensure_inside_root(candidates[0], root)
