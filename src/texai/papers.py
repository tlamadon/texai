"""Reading a paper as text, for the agent: the ``read_paper`` tool.

The agent backs claims with ``%@src`` lines that quote papers (see
:mod:`texai.provenance`), so it has to read them. Neither agent can, on its own:
Claude has only file tools, and its Read of a long PDF needs poppler on the
machine; Codex has a shell but nothing that opens a PDF. So texai reads the PDF
itself, with pypdf, and hands the agent text, page by page.

It also answers the two questions a ``%@src`` needs answered exactly: on which
page of the file a passage is, and the file's hash. A quote is looked for the
way the Peek tab will look for it — letters and digits only, accents and
ligatures unfolded — so a quote this tool finds is one the reader's click will
find too.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .config import AppConfig
from .provenance import (
    PAPERS_DIR,
    ProvenanceError,
    _bib_entry,
    file_sha,
    parse_source,
    serve_path,
)

__all__ = ["READ_PAPER_DESCRIPTION", "READ_PAPER_SCHEMA", "fold", "read_paper"]

# What one answer may carry, so a long paper comes in a few calls rather than
# one that floods the context.
MAX_CHARS = 30_000
# Below this many letters a quote matches too much to mean anything.
MIN_QUOTE = 6
# When the exact words are not there, look for runs of them this long, this far
# apart, and take the page where most are found. Short runs survive a typo or a
# paraphrased word that a long run would not; the agent is then shown the text
# as the PDF has it, and quotes that — which the Peek tab finds exactly.
PROBE = 16
PROBE_STEP = 8
# The share of runs that must be found on one page for it to count as a match.
PROBE_QUORUM = 0.3
# Context shown either side of a found passage.
CONTEXT = 160
SHA_SHOWN = 8

READ_PAPER_DESCRIPTION = """
Read a paper, or any PDF in the project, as text — or find a passage in it.
Use this for every PDF: the Read tool cannot open most of them.

`target` is `@key` for papers/<key>.pdf (the bib key), or a project path to a
PDF. `pages` picks pages, as "3", "3-6" or "2,5", in the PDF file's own count
(1 is the first page of the file, as `p.N` in a `%@src` means); without it you
get the document from the start, as much as fits. `find` is a passage to look
for: the answer says which page it is on and how the text reads there, and
gives a ready `%@src` line with the file's hash. Check every quote with `find`
before writing it into a `%@src`.
""".strip()

READ_PAPER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "@key for papers/<key>.pdf, or a project PDF path"},
        "pages": {"type": "string", "description": 'Pages to read: "3", "3-6" or "2,5"'},
        "find": {"type": "string", "description": "A passage to locate, word for word"},
    },
    "required": ["target"],
}


# ------------------------------------------------------------------ text


def _fold_char(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(
        c.lower() for c in decomposed if not unicodedata.combining(c) and c.isalnum()
    )


def fold(text: str) -> str:
    """Letters and digits, lower-cased, accents and ligatures unfolded: what
    survives any PDF's text extraction. Must agree with ``fold`` in peek.js."""
    return "".join(_fold_char(ch) for ch in text)


def _folded(text: str) -> tuple[str, list[int]]:
    """The folded text, and for each folded character where it came from."""
    out: list[str] = []
    owners: list[int] = []
    for i, ch in enumerate(text):
        for c in _fold_char(ch):
            out.append(c)
            owners.append(i)
    return "".join(out), owners


_text_cache: dict[Path, tuple[int, int, list[str]]] = {}


def _page_texts(path: Path) -> list[str]:
    """Every page's text, cached against the file's size and mtime."""
    stat = path.stat()
    cached = _text_cache.get(path)
    if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
        return cached[2]
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page should not lose the rest
                texts.append("")
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds
        raise ProvenanceError(f"Could not read {path.name} as a PDF: {exc}") from exc
    _text_cache[path] = (stat.st_size, stat.st_mtime_ns, texts)
    return texts


# ------------------------------------------------------------------ finding the file


def _available(config: AppConfig) -> str:
    folder = config.root / PAPERS_DIR
    names = sorted(p.stem for p in folder.glob("*.pdf")) if folder.is_dir() else []
    if not names:
        return f"There is no {PAPERS_DIR}/ folder with PDFs in the project."
    shown = ", ".join(f"@{name}" for name in names[:40])
    more = f" and {len(names) - 40} more" if len(names) > 40 else ""
    return f"Papers available: {shown}{more}."


def _locate(config: AppConfig, target: str) -> tuple[Path, str | None]:
    """The PDF a target names, and its bib key when it is a paper."""
    target = target.strip()
    if target and not target.startswith(("@", "#")):
        # A bare bib key, when there is a paper by that name or it could not be
        # a path; otherwise a path.
        paper = config.root / PAPERS_DIR / f"{target}.pdf"
        if paper.is_file() or not any(ch in target for ch in "/."):
            target = f"@{target}"
    ref = parse_source(target) if target else None
    if ref is None or ref.type == "label":
        raise ProvenanceError("Name a paper as @key, or a PDF by its path in the project.")
    if ref.type == "paper":
        path = config.root / PAPERS_DIR / f"{ref.target}.pdf"
        if not path.is_file():
            raise ProvenanceError(f"No {PAPERS_DIR}/{ref.target}.pdf. {_available(config)}")
        return path, ref.target
    path = serve_path(config, ref.target)
    if not path.is_file():
        raise ProvenanceError(f"No file {ref.target} in the project.")
    if path.suffix.lower() != ".pdf":
        raise ProvenanceError(f"{ref.target} is not a PDF; use the Read tool for it.")
    return path, None


def _pages_wanted(spec: str, count: int) -> list[int]:
    """``"3"``, ``"3-6"``, ``"2,5,9-10"`` as 1-based page numbers in range."""
    wanted: list[int] = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if match is None:
            raise ProvenanceError(f'Pages are written "3", "3-6" or "2,5"; not "{part}".')
        first = int(match.group(1))
        last = int(match.group(2) or first)
        for page in range(min(first, last), max(first, last) + 1):
            if 1 <= page <= count and page not in wanted:
                wanted.append(page)
    if not wanted:
        raise ProvenanceError(f"No such pages: the PDF has {count}.")
    return wanted


# ------------------------------------------------------------------ the tool


def _find(texts: list[str], quote: str) -> dict[str, Any] | None:
    """Where a passage is: the page, the text as it reads there, and whether
    the match is exact or anchored on a run of the quote's words."""
    needle = fold(quote)
    if len(needle) < MIN_QUOTE:
        raise ProvenanceError("That passage is too short to find reliably; quote a few words more.")
    folded = [_folded(text) for text in texts]

    def hit(page: int, start: int, length: int, exact: bool) -> dict[str, Any]:
        text, owners = texts[page], folded[page][1]
        first = owners[start]
        last = owners[min(start + length, len(owners)) - 1] + 1
        return {
            "page": page + 1,
            "exact": exact,
            "match": " ".join(text[first:last].split()),
            "before": " ".join(text[max(0, first - CONTEXT) : first].split()),
            "after": " ".join(text[last : last + CONTEXT].split()),
            "others": [],
        }

    exact = [(i, body.find(needle)) for i, (body, _) in enumerate(folded)]
    exact = [(i, at) for i, at in exact if at != -1]
    if exact:
        found = hit(exact[0][0], exact[0][1], len(needle), True)
        found["others"] = [i + 1 for i, _ in exact[1:]]
        return found

    probe = min(PROBE, len(needle))
    offsets = list(range(0, len(needle) - probe + 1, PROBE_STEP))
    if offsets[-1] != len(needle) - probe:
        offsets.append(len(needle) - probe)
    best: tuple[int, int, int, int] | None = None  # (runs found, page, first offset, at)
    for i, (body, _) in enumerate(folded):
        found = [(offset, body.find(needle[offset : offset + probe])) for offset in offsets]
        found = [(offset, at) for offset, at in found if at != -1]
        if found and (best is None or len(found) > best[0]):
            best = (len(found), i, found[0][0], found[0][1])
    if best is None or best[0] < max(1, PROBE_QUORUM * len(offsets)):
        return None
    _, page, offset, at = best
    start = max(0, at - offset)
    return hit(page, start, min(len(needle), len(folded[page][0]) - start), False)


def read_paper(config: AppConfig, target: str, pages: str = "", find: str = "") -> str:
    """The body of ``read_paper`` for every agent; its answer, as text."""
    try:
        path, key = _locate(config, str(target or ""))
        texts = _page_texts(path)
    except ProvenanceError as exc:
        return f"Could not read that: {exc}"

    relative = path.relative_to(config.root).as_posix()
    sha = file_sha(path)[:SHA_SHOWN]
    count = len(texts)
    head = [f"{relative} — {count} page{'s' if count != 1 else ''}, sha:{sha}"]
    bib = _bib_entry(config, key) if key else None
    if bib:
        who = ", ".join(v for v in (bib.get("author"), bib.get("year")) if v)
        head.append(f"{who}. {bib.get('title', '')}".strip(". "))
    if not any(text.strip() for text in texts):
        head.append(
            "This PDF has no text layer (a scan?), so nothing can be read or found in it."
        )
        return "\n".join(head)

    lines = list(head)
    if find:
        try:
            found = _find(texts, str(find))
        except ProvenanceError as exc:
            return "\n".join([*head, str(exc)])
        if found is None:
            lines.append(
                "The passage is not in this PDF's text. Read the pages around where "
                "you expect it and quote it as the PDF has it."
            )
        else:
            how = "found" if found["exact"] else "not word for word; closest match"
            lines.append(f"Passage {how} on page {found['page']}:")
            lines.append(f"  …{found['before']} «{found['match']}» {found['after']}…")
            if found["others"]:
                pages_list = ", ".join(map(str, found["others"]))
                lines.append(f"It also occurs on page(s) {pages_list}; quote more to pin one down.")
            if found["exact"]:
                target_word = f"@{key}" if key else relative
                quote = found["match"].replace('"', "'")
                lines.append(
                    "As a source line (trim the quote to the words that back the claim):"
                )
                lines.append(f'  %@src {target_word} p.{found["page"]} "{quote}" sha:{sha}')
            else:
                lines.append("Quote it as the PDF reads (the text between « »), not as you had it.")
        if not pages:
            return "\n".join(lines)

    try:
        wanted = _pages_wanted(pages, count) if pages else list(range(1, count + 1))
    except ProvenanceError as exc:
        return "\n".join([*lines, str(exc)])
    used = 0
    shown: list[int] = []
    for page in wanted:
        body = texts[page - 1].strip() or "(no text on this page)"
        if shown and used + len(body) > MAX_CHARS:
            break
        lines.append(f"\n--- page {page} of {count} ---\n{body[:MAX_CHARS]}")
        used += len(body)
        shown.append(page)
    rest = [page for page in wanted if page not in shown]
    if rest:
        lines.append(
            f"\n(Stopped after page {shown[-1]} to keep this short; "
            f'ask for pages="{rest[0]}-{rest[-1]}" for the rest.)'
        )
    return "\n".join(lines)
