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

__all__ = ["LocateError", "locate", "locate_range", "MAX_RANGE_LINES"]

# A change bigger than this is highlighted from its first lines only; the
# mapping costs one subprocess per line and nobody needs 500 bands.
MAX_RANGE_LINES = 60


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


def _dedupe(boxes: list[Any]) -> list[Any]:
    """SyncTeX reports the same rendered line once per contributing source line."""
    seen: set[tuple[int, int, int, int, int]] = set()
    unique = []
    for box in boxes:
        key = (
            box.page,
            round(box.x),
            round(box.y),
            round(box.width),
            round(box.height),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(box)
    return sorted(unique, key=lambda b: (b.page, b.y, b.x))


def locate_range(config: AppConfig, file: str, start: int, end: int) -> dict[str, Any]:
    """Every box produced by a span of source lines.

    Mapping only the first line of a multi-line change highlights whatever that
    line happened to produce — in a wrapped paragraph that is often a different
    rendered line from the one that actually changed, which is why a change
    could appear to cover only its first or only its last line.
    """
    try:
        source = ensure_inside_root(file, config.root)
    except PathOutsideRootError as exc:
        raise LocateError(f"{file} is outside the project root") from exc
    if not source.is_file():
        raise LocateError(f"No such file in the project: {file}")

    first = max(1, int(start))
    last = max(first, int(end))
    truncated = last - first + 1 > MAX_RANGE_LINES
    if truncated:
        last = first + MAX_RANGE_LINES - 1

    boxes: list[Any] = []
    for line in range(first, last + 1):
        try:
            boxes.extend(
                run_synctex_view(
                    config.pdf_path,
                    source,
                    line,
                    root=config.root,
                    executable=config.synctex_executable,
                )
            )
        except SyncTexError:
            continue  # one unmappable line should not lose the rest

    boxes = _dedupe(boxes)
    relative = to_project_relative(source, config.root)
    return {
        "found": bool(boxes),
        "file": relative,
        "line": first,
        "lastLine": last,
        "truncated": truncated,
        "page": boxes[0].page if boxes else None,
        "boxes": [box.as_dict() for box in boxes],
    }
