---
name: review-latex-pdf
description: Compile a LaTeX project with SyncTeX, serve the PDF with texai, and edit the source the user points at in the PDF. Use when the user wants to review a compiled paper, refers to "the selected passage", "this part of the PDF", "the bit I clicked", or asks to fix/rewrite something they are looking at in a PDF.
---

# Review a LaTeX PDF with the user

The user reads the compiled PDF and points at things; you edit the source. The
bridge is `texai`, which turns a Cmd/Ctrl-click in the PDF into a source
file and line via SyncTeX and writes it to
`<project-root>/.texai/current-selection.json`.

> **When to use this skill.** `texai` also has a built-in chat panel that
> drives its own agent session. This skill is for the other mode: the user keeps
> working in *your* session and uses the viewer only to point. If they are
> typing into the panel in the browser, they do not need you for this — but the
> selection file is written either way, so reading it is always valid.

## 1. Find the root LaTeX document

The root file is the one with `\documentclass` and `\begin{document}`:

```bash
grep -rl '\\begin{document}' --include='*.tex' .
```

If several match, prefer, in order: a file named in `latexmkrc`, `Makefile`,
`.vscode/settings.json`, or a build script; then `main.tex`, `paper.tex`, or
`ms.tex`; then the one that `\input`s or `\include`s the others. If it is still
ambiguous, ask the user which file is the root.

## 2. Compile with SyncTeX enabled

Prefer the project's own build command if there is one (`latexmkrc`, `Makefile`,
`justfile`, `build.sh`, a `.vscode` LaTeX recipe). **Check that it passes
`-synctex=1`** — without it there is no `.synctex.gz` and clicks cannot be
mapped. Add the flag if it is missing (e.g. `$pdflatex = 'pdflatex -synctex=1
-interaction=nonstopmode %O %S';` in `latexmkrc`).

Otherwise:

```bash
latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>
```

Run it from the directory the project normally builds in. Confirm both the PDF
and a `.synctex.gz` next to it now exist. If compilation fails, fix the errors
before continuing — `texai` needs a PDF.

## 3. Start texai

Run it in the background and leave it running for the session:

```bash
uv run texai --root <project-root> --pdf <path/to/output.pdf>
```

Tell the user the URL it prints (default <http://127.0.0.1:8765/>) and that
**Cmd-click** (macOS) or **Ctrl-click** records a location — and that selecting
text before clicking also captures the rendered text.

## 4. Read the selection when the user points at the PDF

When the user says "the selected passage", "this part of the PDF", "what I just
clicked", or similar, read:

```bash
cat <project-root>/.texai/current-selection.json
```

```json
{
  "version": 1,
  "updatedAt": "2026-08-13T18:20:31-05:00",
  "pdf": "build/main.pdf",
  "page": 7,
  "pdfPosition": {"x": 241.3, "y": 418.2},
  "source": {"file": "sections/model.tex", "line": 143, "column": 1},
  "selectedText": null
}
```

- `source.file` is relative to the project root; `source.line` is 1-based.
- `selectedText` is the rendered text the user had selected, when they selected
  any — use it to locate the exact sentence, since SyncTeX resolves to a line,
  not a phrase.
- Check `updatedAt`: if it is old, the user may be referring to a newer click
  they have not made yet. Ask rather than editing the wrong place.
- If the file does not exist, ask the user to Cmd/Ctrl-click the spot they mean.

## 5. Edit, recompile, verify

1. Open `source.file` around `source.line`. SyncTeX points at the line that
   produced the clicked box, which can be a line or two off in wrapped
   paragraphs or inside environments — read the surrounding lines and use
   `selectedText` to confirm you have the right spot before editing.
2. Make the requested change.
3. Recompile with the same command as in step 2.
4. Fix any compilation errors it introduces; check the `.log` for the first
   `! ` error and work down from there. Repeat until it builds.
5. Say what you changed and where (`sections/model.tex:143`). The viewer
   reloads the rebuilt PDF on its own, keeping the user's page and scroll
   position, so they can look straight at the result.

## Notes

- Keep one `texai` per project root; restart it if the user switches PDFs.
- Do not edit `.texai/current-selection.json` — it is written by the tool.
- Add `.texai/` to `.gitignore` if the project tracks build output.
