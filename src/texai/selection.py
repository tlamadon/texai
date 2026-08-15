"""Building and atomically persisting the current selection."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "SELECTION_DIRNAME",
    "SELECTION_FILENAME",
    "SELECTION_VERSION",
    "selection_path",
    "build_selection",
    "atomic_write_json",
    "atomic_write_text",
]

SELECTION_DIRNAME = ".texai"
SELECTION_FILENAME = "current-selection.json"
SELECTION_VERSION = 1


def selection_path(root: Path) -> Path:
    """Location of the selection file for a project root."""
    return root / SELECTION_DIRNAME / SELECTION_FILENAME


def build_selection(
    *,
    pdf: str,
    page: int,
    x: float,
    y: float,
    source_file: str,
    line: int,
    column: int,
    word: str | None = None,
    selected_text: str | None = None,
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the selection payload written to ``current-selection.json``.

    ``x``/``y`` are PDF points from the top-left corner of the page (the
    SyncTeX convention). Paths are project-relative POSIX paths.
    """
    stamp = updated_at or datetime.now().astimezone()
    return {
        "version": SELECTION_VERSION,
        "updatedAt": stamp.isoformat(timespec="seconds"),
        "pdf": pdf,
        "page": page,
        "pdfPosition": {"x": round(float(x), 1), "y": round(float(y), 1)},
        "source": {"file": source_file, "line": line, "column": column, "word": word},
        "selectedText": selected_text,
    }


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically.

    The temporary file is created in the destination directory so that
    ``os.replace`` is a same-filesystem rename: a reader either sees the old
    file or the complete new one, never a partial write. Source files get the
    same treatment as the selection file — a crash mid-write must not leave a
    half-rewritten .tex behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; these are ordinary project artifacts.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` as JSON to ``path`` atomically."""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
