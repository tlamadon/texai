"""Which page is which slide: the frames of a deck, placed in its PDF.

:mod:`texai.frames` reads the frames and their blocks out of the source. This
says where each one went. Beamer typesets a frame at its ``\\end{frame}``, so
every glyph on a slide is recorded in the SyncTeX data against that one line —
useless for finding a word, but it names the slide exactly: a page belongs to
the frame whose closing line put the most glyphs on it. Overlays make that
several pages per frame; a ``[fragile]`` frame's body is recorded against a
scratch file, but its title and furniture still come from the closing line.

That is all the server can say. Where on the slide a block sits is worked out
in the browser, which has the page's text and its positions (see
``static/slides.js``).
"""

from __future__ import annotations

from typing import Any

from .config import AppConfig
from .frames import Frame, frame_at, frames_in
from .paragraphs import parsed_synctex
from .paths import to_project_relative
from .snapshots import iter_source_files
from .synctex import synctex_data_file

__all__ = ["frame_for_page", "slide_label", "slide_map", "slide_spot"]

_cache: dict[str, tuple[str, dict[str, Any]]] = {}


def _signature(config: AppConfig, sources: list[Any]) -> str:
    parts: list[str] = []
    data = synctex_data_file(config.pdf_path)
    if data is not None:
        stat = data.stat()
        parts.append(f"{data.name}:{stat.st_mtime_ns}:{stat.st_size}")
    for path in sources:
        try:
            stat = path.stat()
        except OSError:
            continue
        # The size as well as the time: two saves inside one tick of the
        # clock share an mtime, and the second must not be served the first.
        parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def slide_map(config: AppConfig) -> dict[str, Any]:
    """``{"frames": [...]}``: every frame with its file, lines, title, blocks
    and the pages it was typeset on. Empty for a document with no frames.

    Cached against the build and the sources: the blocks are read from the
    files on disk, which move without a rebuild.
    """
    sources = [path for path in iter_source_files(config.root) if path.suffix.lower() == ".tex"]
    key = str(config.pdf_path)
    signature = _signature(config, sources)
    cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    found: list[tuple[str, Frame]] = []
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "frame" not in text:
            continue
        relative = to_project_relative(path, config.root)
        found.extend((relative, frame) for frame in frames_in(text.split("\n")))

    pages: dict[int, list[int]] = {}
    if found:  # a paper never pays for the parse on this account
        counts = parsed_synctex(config)[4]
        best: dict[int, tuple[int, int]] = {}  # page -> (glyphs, frame index)
        for index, (file, frame) in enumerate(found):
            for page, glyphs in counts.get((file, frame.end), {}).items():
                if page not in best or glyphs > best[page][0]:
                    best[page] = (glyphs, index)
        for page, (_glyphs, index) in sorted(best.items()):
            pages.setdefault(index, []).append(page)

    answer = {
        "frames": [
            {"file": file, **frame.as_dict(), "pages": pages.get(index, [])}
            for index, (file, frame) in enumerate(found)
        ]
    }
    _cache[key] = (signature, answer)
    return answer


def frame_for_page(config: AppConfig, page: int) -> dict[str, Any] | None:
    """The frame typeset on ``page``, or ``None`` when the page is not a slide."""
    for frame in slide_map(config)["frames"]:
        if page in frame["pages"]:
            return frame
    return None


def slide_spot(config: AppConfig, file: str, line: int) -> dict[str, Any] | None:
    """Where a line inside a frame is in the PDF: its slide.

    SyncTeX has no record of such a line, so "go to it" would find nothing.
    The slide it is on is known all the same, and is the answer: the frame's
    last page, where every overlay has been uncovered, and one box round what
    the frame put there. The page narrows that to the block itself (see
    ``static/slides.js``). ``None`` when the line is not on a slide that was
    typeset.
    """
    frame = next(
        (
            f
            for f in slide_map(config)["frames"]
            if f["file"] == file and f["start"] <= line <= f["end"] and f["pages"]
        ),
        None,
    )
    if frame is None:
        return None
    page = frame["pages"][-1]
    index, left, right, _words, _counts = parsed_synctex(config)
    top, bottom = index.get((file, frame["end"]), {}).get(page, [0.0, 0.0])
    return {
        "page": page,
        "box": {
            "page": page,
            "x": round(left, 2),
            "y": round(max(0.0, top - 9.0), 2),
            "width": round(max(right - left, 1.0), 2),
            "height": round(max(bottom - top + 12.0, 1.0), 2),
        },
        "frame": {"start": frame["start"], "end": frame["end"], "title": frame["title"]},
    }


def slide_label(config: AppConfig, file: str, line: int) -> str:
    """How to name the slide a source line is on: ``"Title" (file:7-17)``,
    or nothing when the line is not inside a frame."""
    try:
        text = (config.root / file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if "frame" not in text:
        return ""
    frame = frame_at(text.split("\n"), line)
    if frame is None:
        return ""
    where = f"{file}:{frame.start}-{frame.end}"
    return f'"{frame.title}" ({where})' if frame.title else f"untitled ({where})"
