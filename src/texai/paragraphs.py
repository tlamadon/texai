"""Colouring the document by paragraph, in step with the editor's gutter.

The Source editor paints a rotating-palette bar down each blank-line-separated
paragraph (see ``editor.js``). This maps those same paragraphs forward into the
PDF, so a block of source and the block it produced read as the same colour
across the two panes.

The palette index is the paragraph's ordinal within its file, ``% PALETTE`` —
exactly what the editor computes — so the two sides agree without either having
to know what the other is showing. Paragraphs that produced no output (the
preamble, a comment block) simply contribute no bar; the ones that did keep the
index they have in the editor, and therefore its colour.

The forward mapping reads the ``.synctex.gz`` directly rather than shelling out
to ``synctex view`` once per line: a real paper has thousands of paragraphs, and
a subprocess apiece runs into minutes. Every glyph and kern in the file is
tagged with its source ``(file, line)`` and carries a position, so one pass over
the file builds a line-to-position index for the whole document. The result is
cached against the SyncTeX file (rewritten on every build) and the source
mtimes, so only the first request after a change pays for the parse.
"""

from __future__ import annotations

import gzip
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .config import AppConfig
from .paths import to_project_relative
from .snapshots import iter_source_files
from .synctex import synctex_data_file

__all__ = [
    "PARAGRAPH_PALETTE",
    "paragraph_ranges",
    "paragraph_bars",
]

# How many colours the palette rotates through. Must match PARAGRAPH_PALETTE in
# editor.js, or a paragraph would wear one colour in the source and another on
# the page.
PARAGRAPH_PALETTE = 5

# Only prose files carry body text; a .bib or .cls renders nothing to colour.
_PROSE_SUFFIXES = frozenset({".tex"})

# SyncTeX positions are the glyph baselines. A bar drawn between the first and
# last baseline of a paragraph would clip the top line's ascenders and the
# bottom line's descenders, so pad by roughly a line's worth at each end.
_ASCENT_PT = 9.0
_DESCENT_PT = 3.0

# A glyph record: ``x tag,line:h,v`` — an actual character, placed where its ink
# sits. These give the true top and bottom of what a source line typeset (the
# box records' baselines are a whole paragraph's, not a line's).
_GLYPH = re.compile(r"^x(\d+),(\d+):(-?\d+),(-?\d+)")
# A horizontal box: ``(tag,line:h,v:W,H,...``. Its left edge ``h`` is the true
# start of a text line — 8pt to the left of the first glyph — so the bar sits in
# the margin rather than over the first character. Every line repeats the column
# left, while the page's outer container (h = 0) and centred display math are
# one-offs, so the most common left on a page is the text margin.
_HBOX = re.compile(r"^\((\d+),(\d+):(-?\d+),(-?\d+):")
# A sheet opens with its page number on its own line.
_SHEET = re.compile(r"^\{(\d+)")

# One SyncTeX small point is 1/65536 pt; magnification and unit scale that, and
# the parse fills them in from the header before any record is read.
_SP_PER_PT = 65536.0

_cache: dict[str, tuple[str, dict[str, Any]]] = {}


def paragraph_ranges(text: str) -> list[tuple[int, int, int]]:
    """``(index, start_line, end_line)`` for each blank-line-separated paragraph.

    Deliberately the same rule as the editor: a line is a break when it is empty
    after stripping, and a paragraph is a maximal run of the lines between
    breaks. Line numbers are 1-based, matching SyncTeX and the editor's gutter.
    """
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    lines = text.split("\n")
    for number, line in enumerate(lines, start=1):
        if line.strip() == "":
            if start is not None:
                ranges.append((start, number - 1))
                start = None
        elif start is None:
            start = number
    if start is not None:
        ranges.append((start, len(lines)))
    return [(index, first, last) for index, (first, last) in enumerate(ranges)]


def _body_start_line(text: str) -> int:
    """The line where the body begins, or 1 for a file with no preamble.

    A ``\\begin{document}`` marks the split; the files pulled in with ``\\input``
    have none, and every line of those is body.
    """
    for number, line in enumerate(text.split("\n"), start=1):
        if "\\begin{document}" in line:
            return number
    return 1


def _project_file(config: AppConfig, path: str) -> str | None:
    """The project-relative name for a SyncTeX ``Input`` path, or ``None``.

    Only ``.tex`` files inside the root are of interest; the class files, style
    packages and TeX distribution paths that fill most of the ``Input`` table
    are dropped so their tags map to nothing.
    """
    candidate = Path(os.path.normpath(path))
    if candidate.suffix.lower() not in _PROSE_SUFFIXES:
        return None
    try:
        return to_project_relative(candidate, config.root)
    except ValueError:
        return None


def _parse_synctex(
    config: AppConfig,
) -> tuple[dict[tuple[str, int], dict[int, list[float]]], float]:
    """Where each source line landed, read straight from the ``.synctex.gz``.

    Returns two things in PDF points from the top-left of the page — the same
    convention as the click mapping, so the viewer reuses one transform:

    * ``{(file, line): {page: [min_top, max_bottom]}}`` — the vertical span each
      source line occupies, from the glyph baselines. The ascent/descent padding
      that turns them into a covering bar is applied per paragraph, not here.
    * ``left_margin`` — the left edge of the text column, one value for the whole
      document. Bars hang here rather than at each paragraph's own leftmost
      glyph, so a centred equation or an indented one-liner still gets a bar in
      the margin instead of one floating over the middle of the page.
    """
    data = synctex_data_file(config.pdf_path)
    if data is None:
        return {}, 0.0

    tag_files: dict[int, str | None] = {}
    unit = 1.0
    magnification = 1000.0
    x_offset_sp = 0.0
    y_offset_sp = 0.0
    index: dict[tuple[str, int], dict[int, list[float]]] = {}
    left_votes: dict[int, Counter[int]] = {}
    page = 0

    opener = gzip.open if data.suffix == ".gz" else open
    with opener(data, "rt", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not raw:
                continue
            head = raw[0]

            if head == "{":
                sheet = _SHEET.match(raw)
                if sheet:
                    page = int(sheet.group(1))
                continue

            if head in "IMUXY":  # header records, all before the content
                if raw.startswith("Input:"):
                    _, _, rest = raw.partition(":")
                    tag_str, _, path = rest.partition(":")
                    try:
                        tag_files[int(tag_str)] = _project_file(config, path.rstrip("\n"))
                    except ValueError:
                        pass
                elif raw.startswith("Magnification:"):
                    magnification = _header_number(raw, magnification)
                elif raw.startswith("Unit:"):
                    unit = _header_number(raw, unit)
                elif raw.startswith("X Offset:"):
                    x_offset_sp = _header_number(raw, x_offset_sp)
                elif raw.startswith("Y Offset:"):
                    y_offset_sp = _header_number(raw, y_offset_sp)
                continue

            scale = unit * (magnification / 1000.0) / _SP_PER_PT

            if head == "(":
                hbox = _HBOX.match(raw)
                if hbox is None:
                    continue
                left = (float(hbox.group(3)) + x_offset_sp) * scale
                # The page's outer container sits at the origin; a text line sits
                # at the margin. Ignore anything hard against the left edge so the
                # container cannot win the vote.
                if left > 1.0:
                    left_votes.setdefault(page, Counter())[round(left)] += 1
                continue

            glyph = _GLYPH.match(raw)
            if glyph is None:
                continue
            file = tag_files.get(int(glyph.group(1)))
            if file is None:
                continue

            y = (float(glyph.group(4)) + y_offset_sp) * scale
            key = (file, int(glyph.group(2)))
            pages = index.setdefault(key, {})
            slot = pages.get(page)
            if slot is None:
                pages[page] = [y, y]
            else:
                slot[0] = min(slot[0], y)
                slot[1] = max(slot[1], y)

    # A single margin for the whole document. The text column does not move page
    # to page, so the modal line-left across every page is the gutter — and using
    # one value rejects the odd sparse page whose vote a wide table or a centred
    # equation would otherwise win, dragging its bars toward the middle.
    total: Counter[int] = Counter()
    for votes in left_votes.values():
        total.update(votes)
    left_margin = float(total.most_common(1)[0][0]) if total else 0.0
    return index, left_margin


def _header_number(line: str, default: float) -> float:
    try:
        return float(line.partition(":")[2].strip())
    except ValueError:
        return default


def _bars_for_file(
    config: AppConfig,
    path: Path,
    relative: str,
    line_index: dict[tuple[str, int], dict[int, list[float]]],
    left_margin: float,
) -> list[dict[str, Any]]:
    """Every paragraph bar one file contributes, one per page it touches."""
    text = path.read_text(encoding="utf-8", errors="replace")
    body_start = _body_start_line(text)
    bars: list[dict[str, Any]] = []

    for index, first, last in paragraph_ranges(text):
        if last < body_start:
            continue  # preamble: counted for the index, but nothing to draw

        per_page: dict[int, list[float]] = {}
        for line in range(first, last + 1):
            positions = line_index.get((relative, line))
            if not positions:
                continue
            for page, (top, bottom) in positions.items():
                extent = per_page.get(page)
                if extent is None:
                    per_page[page] = [top, bottom]
                else:
                    extent[0] = min(extent[0], top)
                    extent[1] = max(extent[1], bottom)

        for page, (top, bottom) in per_page.items():
            y = top - _ASCENT_PT
            height = (bottom + _DESCENT_PT) - y
            bars.append(
                {
                    "file": relative,
                    "index": index,
                    "palette": index % PARAGRAPH_PALETTE,
                    "page": page,
                    "x": round(left_margin, 2),
                    "y": round(y, 2),
                    "height": round(height, 2),
                }
            )
    return bars


def _signature(config: AppConfig, sources: list[Path]) -> str:
    """What the cached answer depends on: the SyncTeX data, and the source text.

    The PDF's SyncTeX file is rewritten on every build, so its mtime catches a
    rebuild — but the paragraph *ranges* are read from the source on disk, which
    can move without a rebuild, so those mtimes go in too.
    """
    data = synctex_data_file(config.pdf_path)
    parts: list[str] = []
    if data is not None:
        stat = data.stat()
        parts.append(f"{data.name}:{stat.st_mtime_ns}:{stat.st_size}")
    for path in sources:
        try:
            parts.append(f"{path}:{path.stat().st_mtime_ns}")
        except OSError:
            continue
    return "|".join(parts)


def paragraph_bars(config: AppConfig) -> dict[str, Any]:
    """Paragraph bars for the whole document, cached against the build.

    Returns ``{"palette": N, "bars": [...]}`` where each bar carries the source
    file and paragraph it came from, its palette index, and a rectangle in PDF
    points from the page's top-left.
    """
    if synctex_data_file(config.pdf_path) is None:
        return {"palette": PARAGRAPH_PALETTE, "bars": []}

    sources = [
        path
        for path in iter_source_files(config.root)
        if path.suffix.lower() in _PROSE_SUFFIXES
    ]

    key = str(config.pdf_path)
    signature = _signature(config, sources)
    cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    line_index, left_margin = _parse_synctex(config)
    # Only files that actually rendered are worth reading back for paragraphs.
    present = {file for file, _line in line_index}

    bars: list[dict[str, Any]] = []
    for path in sources:
        relative = to_project_relative(path, config.root)
        if relative not in present:
            continue
        bars.extend(_bars_for_file(config, path, relative, line_index, left_margin))

    answer = {"palette": PARAGRAPH_PALETTE, "bars": bars}
    _cache[key] = (signature, answer)
    return answer
