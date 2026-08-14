"""Locating a source line in the rendered PDF, so the view can be moved to it.

This is the same forward mapping the change markers use (`synctex view`),
exposed on its own so two callers can share it: the browser, when you click a
`file:line` reference in the chat, and the agent, when you ask to be taken to a
table or a definition.
"""

from __future__ import annotations

from typing import Any

from .config import AppConfig
from .paths import PathOutsideRootError, ensure_inside_root, to_project_relative
from .synctex import SyncTexError, run_synctex_view

__all__ = ["LocateError", "locate"]


class LocateError(RuntimeError):
    """The location could not be resolved (bad path, or SyncTeX unavailable)."""


def locate(config: AppConfig, file: str, line: int) -> dict[str, Any]:
    """Where a source line landed in the PDF.

    Returns the page and the boxes it produced. ``found`` is ``False`` when
    SyncTeX has no record of that line — an unused ``\\newcommand``, a comment,
    or a line inside an environment it does not track — which is a normal
    answer rather than an error.
    """
    try:
        source = ensure_inside_root(file, config.root)
    except PathOutsideRootError as exc:
        raise LocateError(f"{file} is outside the project root") from exc

    if not source.is_file():
        raise LocateError(f"No such file in the project: {file}")

    try:
        boxes = run_synctex_view(
            config.pdf_path,
            source,
            max(1, int(line)),
            root=config.root,
            executable=config.synctex_executable,
        )
    except SyncTexError as exc:
        raise LocateError(str(exc)) from exc

    relative = to_project_relative(source, config.root)
    if not boxes:
        return {"found": False, "file": relative, "line": int(line), "page": None, "boxes": []}

    return {
        "found": True,
        "file": relative,
        "line": int(line),
        "page": boxes[0].page,
        "boxes": [box.as_dict() for box in boxes],
    }
