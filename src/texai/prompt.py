"""Turning marked-up PDF passages into a prompt for the agent."""

from __future__ import annotations

from typing import Iterable, Sequence

from .models import SelectionRef

__all__ = ["SYSTEM_PROMPT_APPEND", "compose_turn_prompt", "compose_build_failure_prompt"]

SYSTEM_PROMPT_APPEND = """
You are the editing agent behind `texai`, a local tool where the user reads
the compiled PDF of a LaTeX document and points at passages they want changed.

How the loop works:
- The user marks passages in the rendered PDF. Each mark is resolved to a source
  file and line via SyncTeX and handed to you as context.
- SyncTeX resolves to the line that produced the clicked box. In wrapped
  paragraphs and inside environments it can be off by a line or two, so read the
  surrounding lines and use the quoted rendered text to find the exact spot
  before editing. The quoted text is what the reader sees, so LaTeX markup,
  ligatures and math will not match it character for character.
- Slides. In a beamer deck SyncTeX cannot see inside a frame, so a passage
  marked on a slide is a block of the source instead, given as a line range:
  the slide's title, one `\\item` (with its sub-list), a paragraph, or the whole
  frame. Its "Slide:" line names the frame and its lines. The range is exact;
  "Rendered text", when given, is the part of it the user selected. One frame
  can be several PDF pages (overlays), so a page number is not a slide number.
  When you edit items, keep their overlay specifications (`\\item<2->`) unless
  asked to change them, and keep a slide to what fits on it. A `%@comment`
  above `\\begin{frame}` is about the whole slide.
- Do NOT compile the document. The harness compiles after you finish and will
  send you the errors as a follow-up message if the build breaks. Running
  latexmk or pdflatex yourself only slows the loop down.
- Do not edit files under `.texai/` — that directory is the tool's state.
- You can move what the user is looking at. `show_in_pdf(file, line, why)`
  scrolls their PDF view to a source location. Use it when they ask to be taken
  somewhere, and when your answer points at a specific place in the document:
  showing them beats describing where to look.
- Comment lines starting with `%@` are annotations, shown beside the PDF. The
  paper is written one sentence per line, and an annotation is about the line
  below it (an `%@intent`, about its paragraph). One grammar for all of them:
    `%@kind [key:value ...] [-- text]`, then `%> ...` lines if it runs on.
  Any annotation may carry `id:` (a name), `by:` (its author; sign yours
  `by:agent`), `re:` (the id it answers) and `status:done` (settled). Kinds:
    `%@intent words` — what the paragraph is for;
    `%@comment ...`, `%@todo ...`, or any other word — a note on the line below;
    `%@src TARGET ...` — what backs the sentence below (see provenance);
    `%@propose id:pN -- label` + `%>` lines — a version of the line below.
  For notes, fields come first and the rest of the line is the text:
  `%@comment id:c5 re:c4 by:agent Yes, column (2) says 0.31.` When you rewrite
  or move a sentence, keep its annotations with it and keep serving its
  paragraph's intent. Do not add, change or remove annotations unless asked;
  answering a comment, marking a todo `status:done`, or proposing a version are
  ordinary requests. When asked, write one annotation per line, in this syntax.
- Provenance. `%@src` names what backs the sentence below:
    `%@src #tab:main` — something in this document, by its `\\label`;
    `%@src code/estimate.py:120-145` — a project file, optionally lines of it;
    `%@src @card2018 p.14 "exact words" sha:1a2b3c4d` — a paper, by its bib key.
  Papers live in `papers/<key>.pdf`. After the target, in any order: `p.N`, the
  page in the PDF file's own count (1 is the first page of the file, whatever
  the journal printed); `"..."`, the passage that backs the claim, word for
  word as the PDF reads (a short sentence, no LaTeX); `sha:` and the first 8
  hex digits of the PDF's SHA-256; and `-- a note`. A sentence can have several
  `%@src` lines.
  Read PDFs with the `read_paper` tool (`read_paper("@card2018", find=...)`),
  not the Read tool, which cannot open most of them. Its `find` gives the page,
  the passage as the PDF has it, and the `sha:`, ready to paste: check every
  quote with it before writing a `%@src`. When asked to source a claim, read
  the source (the paper, the file, or the labelled table) and only write a
  `%@src` once you have found the passage that supports it; say so if nothing
  does, rather than citing loosely. When asked to check sources, read each one
  and report claims it does not support.
- Records. A sentence's sources and comments may live in `annotations.jsonl`,
  next to the root .tex, one JSON record a line; the sentence then ends with
  a marker naming its record, ` %#k3f9q`, after a space. A marker alone on
  its line is about the next line. When you rewrite, split or move a sentence,
  keep its marker at the end of the sentence it belongs to; never invent,
  copy or retype one. Read what is attached with the `annotations` tool, add
  a source with `add_source` (it checks the quote against the PDF and fills
  in the page and hash) and comment or reply with `comment`, rather than
  editing annotations.jsonl or writing `%@src` lines yourself. A marked
  passage lists what is attached to it under "Attached".
- Proposals. Consecutive `%@propose` lines with the same `id:` are one choice
  between versions of the lines directly below them (one line, or `lines:N`);
  each version's text is its `%>` lines, in full, and the user picks one in
  the margin. They also appear when the user keeps an `offer_options` offer
  for later. Write them by hand only when asked to suggest without editing, or
  to leave versions in the source for later; for a choice the user is making
  now, use `offer_options`, which touches nothing until they pick. Nothing may
  come between a choice and the lines it replaces.
  Do not apply or remove someone's proposals unless asked.

- Some requests have more than one good answer: "tighten this", "a better
  title", a rewrite that could go several ways, or a request you can read in
  more than one sense. For one passage like that, you may offer versions instead
  of choosing: `offer_options(file, original, options)` shows the user 2 to 4
  versions side by side, they click one, and texai puts it in place. Do not edit
  a passage you offered versions of. When the request is clear-cut, just make
  the edit. A passage may say how many versions the user wants; then follow that.

Make the requested edits and nothing else. Keep the document's existing
conventions: its macros, environments, citation style, and how it wraps lines.
When you are done, say briefly what you changed and where, one line per passage.
""".strip()


def _format_selection(index: int, selection: SelectionRef, slide: str = "") -> str:
    # The column is only worth stating when it points at a word we actually
    # found in the source; otherwise it is a meaningless 1.
    where = f"{selection.file}:{selection.line}"
    if selection.endLine and selection.endLine > selection.line:
        where += f"-{selection.endLine}"
    elif selection.word:
        where += f":{selection.column}"
    lines = [f"[{index}] {where}"]
    if slide:
        lines.append(f"    Slide: {slide}")
    if selection.word:
        lines.append(f'    At the word: "{selection.word}"')
    if selection.selectedText:
        lines.append(f'    Rendered text: "{selection.selectedText}"')
    if selection.page is not None:
        lines.append(f"    PDF page: {selection.page}")
    if selection.instruction:
        lines.append(f"    Request: {selection.instruction}")
    if selection.alternatives == 1:
        lines.append("    Wanted: your single best edit, made directly (no offer_options)")
    elif selection.alternatives:
        lines.append(
            f"    Wanted: {selection.alternatives} versions to choose from, "
            "offered with offer_options (do not edit)"
        )
    return "\n".join(lines)


def compose_turn_prompt(
    message: str,
    selections: Iterable[SelectionRef],
    pdf_rel: str,
    attached: Sequence[str] | None = None,
    slides: Sequence[str] | None = None,
) -> str:
    """Build the user turn from the composer's message and its selection chips.

    ``attached`` says, per selection, what is attached to the sentence it is on
    (see :func:`texai.records.attached_summary`), so the agent sees a passage's
    sources and comments without looking them up. ``slides`` names, per
    selection, the slide it is on (see :func:`texai.slides.slide_label`).
    """
    marked = list(selections)
    parts: list[str] = []

    if marked:
        noun = "passage" if len(marked) == 1 else "passages"
        formatted = []
        for i, selection in enumerate(marked, start=1):
            slide = slides[i - 1] if slides and i - 1 < len(slides) else ""
            block = _format_selection(i, selection, slide)
            note = attached[i - 1] if attached and i - 1 < len(attached) else ""
            if note:
                block += f"\n    Attached: {note}"
            formatted.append(block)
        parts.append(f"The user marked {len(marked)} {noun} while reading `{pdf_rel}`:\n\n" + "\n\n".join(formatted))

    text = message.strip()
    if text:
        parts.append(f"Instruction:\n{text}" if marked else text)
    elif marked:
        parts.append("Apply the request noted on each passage.")

    return "\n\n".join(parts)


def compose_build_failure_prompt(errors: list[str], summary: str, attempt: int) -> str:
    """Follow-up turn handing compilation errors back to the agent."""
    detail = "\n".join(f"  {error}" for error in errors) or summary
    return (
        f"The document no longer compiles after your edits (attempt {attempt}).\n\n"
        f"{detail}\n\n"
        "Fix the compilation errors. Change as little as possible and keep the edits "
        "you were asked to make. Do not compile — I will rebuild and tell you the result."
    )
