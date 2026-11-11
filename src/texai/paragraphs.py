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

The same pass places the author's ``%@`` annotations (see :mod:`texai.annotations`):
an intent beside its paragraph's bar, anything else beside the line it is about.
Each carries its kind's ``place``, which is all the browser draws by; a
``%@src`` also carries its parsed target (see :mod:`texai.provenance`), and the
versions of one ``%@propose`` choice travel as one item (see
:mod:`texai.proposals`).
"""

from __future__ import annotations

import gzip
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .annotations import PROPOSE, SOURCE, is_annotation_line, kind_of, parse_annotations
from .config import AppConfig
from .paths import to_project_relative
from .proposals import proposals_in
from .provenance import ProvenanceError, bib_entries, source_from_dict, source_ref
from .records import load_records, markers_in, records_path, sentence_line
from .snapshots import iter_source_files
from .synctex import synctex_data_file

__all__ = [
    "PARAGRAPH_PALETTE",
    "body_start_line",
    "paragraph_ranges",
    "paragraph_bars",
    "parsed_synctex",
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
# one-offs, so the most common left on a page is the text margin. Likewise every
# full line is the column's width ``W``, so the most common ``h + W`` is its right
# edge, where the notes hang.
_HBOX = re.compile(r"^\((\d+),(\d+):(-?\d+),(-?\d+):(-?\d+),")
# A sheet opens with its page number on its own line.
_SHEET = re.compile(r"^\{(\d+)")

# One SyncTeX small point is 1/65536 pt; magnification and unit scale that, and
# the parse fills them in from the header before any record is read.
_SP_PER_PT = 65536.0

# How much of the sentence a source backs travels with it, to tell one source
# in a paragraph's list from the next.
_BACKS_CHARS = 120
# A comment runs from an unescaped % to the end of its line: a record's marker,
# say, which is not part of the sentence it sits on.
_COMMENT = re.compile(r"(?<!\\)%.*$")
# How long a sentence's last word is taken to be, when the page cannot say.
_LAST_WORD_PT = 20.0

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


def body_start_line(text: str) -> int:
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


# Roughly where a source line's words fall on each rendered line it reaches:
# (page, baseline) -> [first word start, last word start]. Only roughly — a word
# can be tagged with the line after the one it is on — so this says which
# rendered lines a sentence is on, and the page finds its words in its text.
Words = dict[tuple[str, int], dict[tuple[int, float], list[float]]]


# How many glyphs each source line put on each page: {(file, line): {page: n}}.
Counts = dict[tuple[str, int], dict[int, int]]

_parsed: dict[str, tuple[str, Any]] = {}


def parsed_synctex(
    config: AppConfig,
) -> tuple[dict[tuple[str, int], dict[int, list[float]]], float, float, Words, Counts]:
    """:func:`_parse_synctex`, kept until the next build rewrites the file.

    The paragraph bars and the slide map (see :mod:`texai.slides`) both start
    from the same pass over the SyncTeX data; one of them pays for it.
    """
    data = synctex_data_file(config.pdf_path)
    if data is None:
        return {}, 0.0, 0.0, {}, {}
    stat = data.stat()
    signature = f"{data}:{stat.st_mtime_ns}:{stat.st_size}"
    key = str(config.pdf_path)
    cached = _parsed.get(key)
    if cached is None or cached[0] != signature:
        cached = (signature, _parse_synctex(config))
        _parsed[key] = cached
    return cached[1]


def _parse_synctex(
    config: AppConfig,
) -> tuple[dict[tuple[str, int], dict[int, list[float]]], float, float, Words, Counts]:
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
    * ``right_margin`` — the right edge of the text column, found the same way.
    * ``words`` — for each source line, on each rendered line it reached, where
      its words start and end (see :data:`Words`).
    * ``counts`` — how many glyphs each source line put on each page (see
      :data:`Counts`): on a beamer deck, which slide a page is.
    """
    data = synctex_data_file(config.pdf_path)
    if data is None:
        return {}, 0.0, 0.0, {}, {}

    tag_files: dict[int, str | None] = {}
    unit = 1.0
    magnification = 1000.0
    x_offset_sp = 0.0
    y_offset_sp = 0.0
    index: dict[tuple[str, int], dict[int, list[float]]] = {}
    words: Words = {}
    counts: Counts = {}
    left_votes: dict[int, Counter[int]] = {}
    right_votes: Counter[int] = Counter()
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
                    width = float(hbox.group(5)) * scale
                    if width > 1.0:
                        right_votes[round(left + width)] += 1
                continue

            glyph = _GLYPH.match(raw)
            if glyph is None:
                continue
            file = tag_files.get(int(glyph.group(1)))
            if file is None:
                continue

            x = (float(glyph.group(3)) + x_offset_sp) * scale
            y = (float(glyph.group(4)) + y_offset_sp) * scale
            runs = words.setdefault((file, int(glyph.group(2))), {})
            run = runs.get((page, round(y, 2)))
            if run is None:
                runs[(page, round(y, 2))] = [x, x]
            else:
                run[0] = min(run[0], x)
                run[1] = max(run[1], x)
            key = (file, int(glyph.group(2)))
            on_page = counts.setdefault(key, {})
            on_page[page] = on_page.get(page, 0) + 1
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
    right_margin = float(right_votes.most_common(1)[0][0]) if right_votes else 0.0
    return index, left_margin, right_margin, words, counts


def _header_number(line: str, default: float) -> float:
    try:
        return float(line.partition(":")[2].strip())
    except ValueError:
        return default


def _line_top(
    positions: dict[int, list[float]] | None,
) -> tuple[int, float] | None:
    """The first page a source line typeset on, and the top of it there."""
    if not positions:
        return None
    page = min(positions)
    return page, positions[page][0] - _ASCENT_PT


def _map_file(
    path: Path,
    relative: str,
    line_index: dict[tuple[str, int], dict[int, list[float]]],
    left_margin: float,
    right_margin: float,
    words: Words | None = None,
    records: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every paragraph bar one file contributes, one per page it touches, and
    the annotations it carries, each placed beside the paragraph it belongs to.
    A source also carries where the sentence it backs is, to be underlined."""
    text = path.read_text(encoding="utf-8", errors="replace")
    body_start = body_start_line(text)
    ranges = paragraph_ranges(text)
    bars: list[dict[str, Any]] = []
    # Where each paragraph that typeset anything begins: (page, top).
    starts: dict[int, tuple[int, float]] = {}

    for index, first, last in ranges:
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
                    "width": round(right_margin - left_margin, 2),
                    "height": round(height, 2),
                }
            )
        # The first line in source order, not the earliest page any line
        # touched: a page shipped out mid-paragraph tags its footer with
        # whatever line TeX was reading, which can be a page too early.
        for line in range(first, last + 1):
            found = _line_top(line_index.get((relative, line)))
            if found is not None:
                starts[index] = found
                break

    annotations = parse_annotations(text)
    lines = text.split("\n")
    # Each annotation is one item on the page, except proposals: the versions
    # of one choice are drawn, and picked from, together.
    items: list[tuple[int, int, str, dict[str, Any]]] = []
    for note in annotations:
        if note.kind == PROPOSE:
            continue
        item = note.as_dict()
        if note.kind == SOURCE:
            # A provenance mark: an underline on the sentence it backs, and a
            # mark in the margin that opens it in the peek.
            ref = source_ref(note)
            if ref is None:
                continue
            item["source"] = ref.as_dict()
            backed, _ = _backed_line(lines, note.end)
            item.update(_sentence(lines, backed, relative, words, right_margin))
        items.append((note.line, note.end, note.place, item))

    # Records in annotations.jsonl, by the markers on their sentences: drawn
    # exactly as the inline annotations are, and placed beside the sentence.
    for marker, record_id, alone in markers_in(text):
        record = (records or {}).get(record_id)
        about = sentence_line(lines, marker, alone)
        if record is None or not about:
            continue
        common = {"line": marker, "end": marker, "record": record_id}
        sentence = _sentence(lines, about, relative, words, right_margin)
        for source in record.get("sources", []):
            try:
                ref = source_from_dict(source)
            except ProvenanceError:
                continue
            item = {"kind": SOURCE, "place": "marker", "text": ref.note, "source": ref.as_dict()}
            items.append((about, about - 1, "marker", {**item, **common, **sentence}))
        for comment in record.get("comments", []):
            kind = comment.get("kind") or "comment"
            thread_id = f"{record_id}:{comment.get('id')}"
            items.append((about, about - 1, kind_of(kind).place, {
                **common,
                "kind": kind,
                "place": kind_of(kind).place,
                "id": thread_id,
                "cid": comment.get("id"),
                "by": comment.get("by", ""),
                "status": comment.get("status", ""),
                "text": comment.get("text", ""),
            }))
            for reply in comment.get("replies", []):
                items.append((about, about - 1, "note", {
                    **common,
                    "kind": "comment",
                    "place": "note",
                    "id": f"{record_id}:{reply.get('id')}",
                    "cid": reply.get("id"),
                    "re": thread_id,
                    "by": reply.get("by", ""),
                    "text": reply.get("text", ""),
                }))
    for choice in proposals_in(text, annotations):
        item = {"kind": PROPOSE, "place": "proposal", **choice.as_dict()}
        items.append((choice.line, choice.end, "proposal", item))
    items.sort(key=lambda entry: entry[0])

    notes: list[dict[str, Any]] = []
    for line, end, place, item in items:
        if line < body_start:
            continue  # the preamble has nothing on the page to hang it beside
        # Its own paragraph, or — for a block of annotations on their own — the
        # first one below that typeset something.
        own = next((r for r in ranges if r[1] <= line <= r[2]), None)
        if own is None:
            continue
        target = next((r for r in ranges[own[0] :] if r[0] in starts), None)
        if target is None:
            continue
        index, first, last = target

        spot = starts[index]
        if place != "intent":
            # Beside the line it is about: the first one after it that put
            # something on the page, else the last one before it.
            after = range(max(first, end + 1), last + 1)
            before = range(min(last, line - 1), first - 1, -1)
            for candidate in (*after, *before):
                found = _line_top(line_index.get((relative, candidate)))
                if found is not None:
                    spot = found
                    break

        notes.append(
            {
                **item,
                "file": relative,
                "index": index,
                "palette": index % PARAGRAPH_PALETTE,
                "page": spot[0],
                "x": round(left_margin, 2),
                "right": round(right_margin, 2),
                "y": round(spot[1], 2),
            }
        )
    return bars, notes


def _sentence(
    lines: list[str],
    backed: int,
    relative: str,
    words: Words | None,
    right_margin: float,
) -> dict[str, Any]:
    """What a source carries about the sentence it backs: its line and text,
    the start of it to show, and roughly where it is, for the underline."""
    if not backed:
        return {"backs": ""}
    text = _COMMENT.sub("", lines[backed - 1]).strip()
    short = " ".join(text.split())
    if len(short) > _BACKS_CHARS:
        short = short[: _BACKS_CHARS - 1].rstrip() + "\u2026"
    fields: dict[str, Any] = {"sentence": backed, "sentenceText": text, "backs": short}
    segments = _segments((words or {}).get((relative, backed)), right_margin)
    if segments:
        fields["segments"] = segments
    return fields


def _backed_line(lines: list[str], end: int) -> tuple[int, str]:
    """The first line of text below an annotation ending at line ``end``: with
    one sentence per line, the sentence it is about. Its number, and its text
    shortened, as written; ``(0, "")`` when the paragraph ends first."""
    for number in range(end + 1, len(lines) + 1):
        line = lines[number - 1]
        if not line.strip():
            return 0, ""
        if is_annotation_line(line):
            continue
        text = " ".join(line.split())
        short = text if len(text) <= _BACKS_CHARS else text[: _BACKS_CHARS - 1].rstrip() + "…"
        return number, short
    return 0, ""


def _segments(
    runs: dict[tuple[int, float], list[float]] | None, right_margin: float
) -> list[dict[str, Any]]:
    """Roughly where a source line is on the page: one segment per rendered
    line it reaches, from its first word's start to its last word's start, or
    to the margin on a line it runs on from. The page finds the sentence's own
    words in its text near here (``sentenceText``) and underlines those; this is
    what it underlines when it cannot."""
    if not runs:
        return []
    keys = sorted(runs)
    segments = []
    for i, (page, base) in enumerate(keys):
        first_word, last_word = runs[(page, base)]
        final = i == len(keys) - 1
        segments.append(
            {
                "page": page,
                "top": round(base - _ASCENT_PT, 2),
                "base": round(base, 2),
                "x0": round(first_word, 2),
                "x1": round(last_word + _LAST_WORD_PT if final else right_margin, 2),
            }
        )
    return segments


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

    Returns ``{"palette": N, "bars": [...], "annotations": [...]}`` where each
    bar carries the source file and paragraph it came from, its palette index,
    and a rectangle in PDF points from the page's top-left. Each annotation
    carries its source line, kind and text, the paragraph it belongs to, and
    where it goes: a page, a top, and the text column's left and right edges.
    """
    if synctex_data_file(config.pdf_path) is None:
        return {"palette": PARAGRAPH_PALETTE, "bars": [], "annotations": []}

    every = iter_source_files(config.root)
    sources = [path for path in every if path.suffix.lower() in _PROSE_SUFFIXES]
    # The .bib files only name the papers sources cite, but a renamed title
    # should still reach the margin without a rebuild.
    bibs = [path for path in every if path.suffix.lower() == ".bib"]

    key = str(config.pdf_path)
    signature = _signature(config, [*sources, *bibs, records_path(config)])
    cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    line_index, left_margin, right_margin, words, _counts = parsed_synctex(config)
    records = load_records(config)
    # Only files that actually rendered are worth reading back for paragraphs.
    present = {file for file, _line in line_index}

    bars: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for path in sources:
        relative = to_project_relative(path, config.root)
        if relative not in present:
            continue
        file_bars, file_notes = _map_file(
            path, relative, line_index, left_margin, right_margin, words, records
        )
        bars.extend(file_bars)
        notes.extend(file_notes)

    # Papers by their bib entry, so a paragraph's list of sources can say what
    # each one is rather than give its key.
    keys = {
        note["source"]["target"]
        for note in notes
        if note.get("source", {}).get("type") == "paper"
    }
    entries = bib_entries(config, keys)
    for note in notes:
        source = note.get("source")
        if source and source["type"] == "paper" and source["target"] in entries:
            note["bib"] = entries[source["target"]]

    # Records and markers that have lost each other: a sentence deleted with
    # its marker, a marker copied without its record, a merge half resolved.
    pointed: dict[str, list[str]] = {}
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, record_id, _alone in markers_in(text):
            pointed.setdefault(record_id, []).append(f"{to_project_relative(path, config.root)}:{line}")
    detached = {
        "records": sorted(record_id for record_id in records if record_id not in pointed),
        "markers": sorted(
            where for record_id, places in pointed.items() if record_id not in records for where in places
        ),
    }

    answer = {
        "palette": PARAGRAPH_PALETTE,
        "bars": bars,
        "annotations": notes,
        "detached": detached,
    }
    _cache[key] = (signature, answer)
    return answer
