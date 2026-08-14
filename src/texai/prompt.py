"""Turning marked-up PDF passages into a prompt for the agent."""

from __future__ import annotations

from typing import Iterable

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
- Do NOT compile the document. The harness compiles after you finish and will
  send you the errors as a follow-up message if the build breaks. Running
  latexmk or pdflatex yourself only slows the loop down.
- Do not edit files under `.texai/` — that directory is the tool's state.
- You can move what the user is looking at. `show_in_pdf(file, line, why)`
  scrolls their PDF view to a source location. Use it when they ask to be taken
  somewhere, and when your answer points at a specific place in the document:
  showing them beats describing where to look.

Make the requested edits and nothing else. Keep the document's existing
conventions: its macros, environments, citation style, and how it wraps lines.
When you are done, say briefly what you changed and where, one line per passage.
""".strip()


def _format_selection(index: int, selection: SelectionRef) -> str:
    lines = [f"[{index}] {selection.file}:{selection.line}"]
    if selection.selectedText:
        lines.append(f'    Rendered text: "{selection.selectedText}"')
    if selection.page is not None:
        lines.append(f"    PDF page: {selection.page}")
    if selection.instruction:
        lines.append(f"    Request: {selection.instruction}")
    return "\n".join(lines)


def compose_turn_prompt(
    message: str, selections: Iterable[SelectionRef], pdf_rel: str
) -> str:
    """Build the user turn from the composer's message and its selection chips."""
    marked = list(selections)
    parts: list[str] = []

    if marked:
        noun = "passage" if len(marked) == 1 else "passages"
        parts.append(
            f"The user marked {len(marked)} {noun} while reading `{pdf_rel}`:\n\n"
            + "\n\n".join(_format_selection(i, s) for i, s in enumerate(marked, start=1))
        )

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
