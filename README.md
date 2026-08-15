# texai

A canvas for LaTeX: read the compiled PDF on the left, talk to a coding agent on
the right. Cmd/Ctrl-click a passage to attach it to your message; the agent edits
the source, `texai` rebuilds the document, and the page reloads in place.
Alt-click instead and you get the LaTeX source at that spot in an editor, where
saving recompiles. When you like where it has got to, commit it without leaving
the page.

The bridge that makes it work is SyncTeX — a pixel on page 7 becomes
`sections/model.tex:143`, which is what the agent actually needs. SyncTeX stops
at the line; matching the clicked word against the source gets it to
`sections/model.tex:143:15`.

## Install

```bash
pip install texai
```

That is everything, agent included. The one piece it cannot install for you is
the **`claude` CLI**, which the agent SDK drives and which supplies the
credentials — see [Turning on the agent](#turning-on-the-agent). Without it the
viewer, the SyncTeX bridge and the selection file all still work, and the chat
panel says what is missing.

(`pip install "texai[agent]"` still works. The extra is empty now that the SDK
is a plain dependency, and is kept only so the 0.1.0 install line does not
break.)

## Quick start

From a checkout of this repo:

```bash
uv sync
uv run texai --root ./example --pdf ./example/main.pdf
```

Then open <http://127.0.0.1:8765/>.

The example is not compiled yet; build it first (needs a TeX installation):

```bash
cd example && latexmk -pdf -synctex=1 -interaction=nonstopmode main.tex && cd ..
uv run texai --root ./example --pdf ./example/main.pdf --open
```

On a real project:

```bash
uv run texai --root /path/to/paper --pdf build/main.pdf --open
```

### Turning on the agent

The Claude Agent SDK installs with texai. The remaining requirement is the
**`claude` CLI**, which the SDK drives:

```bash
uv sync                    # the agent SDK comes with it
uv run texai --root . --pdf build/main.pdf --open
```

Without the CLI the viewer, the SyncTeX bridge and the selection file all still
work, and the panel names the missing piece rather than failing vaguely.

The agent runs as a normal local Claude Code session and uses whatever
credentials `claude` is already logged in with — your subscription, if that is
how you use Claude Code. No API key needed, and turns count against that
account exactly as they would in the terminal.

(The Agent SDK docs do restrict *redistributing* subscription login: shipping
this to other people who sign in with their own Claude accounts needs approval
from Anthropic, and API-key auth otherwise. That is about distribution, not
about running it yourself.)

## The loop

1. **Attach.** Cmd-click (macOS) / Ctrl-click a passage. It becomes a chip on
   the composer with its `file:line` and, if you selected text first, the
   rendered text. Each chip takes its own note, so you can mark up several
   passages and send them as one batch.
2. **Send.** The chips plus your message become one prompt.
3. **Edit.** The agent reads and edits the source. Its text, tool calls and
   thinking show up live in the panel.
4. **Build.** `texai` compiles — not the agent. If the build breaks, the
   errors go back to the agent and it tries again (up to three attempts).
5. **See it.** On success the viewer reloads the rebuilt PDF, keeping your page,
   zoom and scroll position. The turn shows which files changed, with a diff you
   can expand and a **Revert** button.

If the document still does not compile after the retries, the whole turn is
rolled back. A half-applied batch on top of a broken document is worse than
nothing.

Turns that only answer a question — no file changed — skip the build entirely.

### Inline changes on the page

Toggle **Show changes** in the toolbar (off by default, remembered). After a
turn, every changed region is forward-mapped through `synctex view` — the click
direction run backwards — and drawn as a band over the text it produced. Click
one:

```
sections/model.tex:9
- labour. The elasticity $\alpha$ is the parameter a reader is most likely to
+ labor. The elasticity $\alpha$ is the parameter a reader is most likely to
                                                     [ Accept ]  [ Reject ]
```

Each change also gets a card under it showing what the sentence used to say:

```
sections/model.tex:9                          [Accept] [Reject] [x]
  …is the parameter a̶ ̶r̶e̶a̶d̶e̶r̶ ̶i̶s̶ most l̶i̶k̶e̶l̶y̶ ̶t̶o̶ ̶a̶r̶g̶u̶e̶ ̶w̶i̶t̶h̶,̶ which makes…
  …is the parameter most **often contested,** which makes…
```

Only the words that actually differ are struck through (red) or emphasised
(green); the surrounding text is kept, dimmed, for context. Striking a whole
wrapped line when three words changed just makes you hunt for the difference.

Clicking a highlight always shows its card, next to the band you clicked. The
card's `x` hides it again, and clicking the highlight brings it back — including
for changes you have already accepted, which start collapsed. Nothing blocks:
ignore them and keep working if you would rather.

Two things to know about the card. The removed text **is not in the PDF** —
LaTeX never typeset it, because it was removed — so it is drawn as an
annotation in the browser's font, and math appears as source (`$\alpha$`).
And the card overlays the page, so it covers the lines beneath it until you
collapse it.

### One review, many turns

Changes accumulate. A second turn does not retire the first one's changes —
everything stays pending until you deal with it, and the toolbar counts what is
outstanding (`Changes (3)`). **Accept all** closes the review and starts a fresh
one from the current state.

That works because pending changes are computed against a **session baseline**,
not per turn: one coherent set, recomputed from the baseline every time. Showing
several turns' own diffs at once would be wrong rather than merely unimplemented
— turn 1's changes describe a file turn 2 has since edited, so rejecting an old
one would quietly undo newer work too.

Each turn's badge follows what became of its edits:

| Badge | Meaning |
| --- | --- |
| `applied` | edits landed and compiled, still pending review |
| `accepted` | every change from that turn was accepted |
| `rejected` | its changes are gone from the pending set — rolled back |
| `mixed` | some accepted, some still pending |
| `answered` | a question; nothing changed |
| `reverted` | the build could not be fixed, so the turn undid itself |

**Reject** rolls back that one change and leaves the rest of the turn alone, so
you can keep three edits out of four from a batch. It is a reconstruction, not a
patch: the file is rebuilt from the turn's snapshot with the rejected region
taken from the old text, then recompiled. Hunk ids are content-derived rather
than positional, so rejecting one does not renumber the others.

**Accept** just marks it reviewed. Nothing blocks — ignore the markers and keep
working if you would rather.

Where it is approximate, honestly:

- Highlights are **word-level where they can be**. SyncTeX only reports the line
  box — the full column width — so the changed words are matched against the
  rendered text layer and highlighted directly. When they cannot be found the
  whole line is banded instead, which happens for changes that are pure LaTeX
  (`$\alpha$` renders as a glyph, not as those characters), for hyphenated
  words split across lines, and for pure deletions, which have no new text to
  point at.
- Every source line of a change is mapped, not just the first, so a rewritten
  paragraph is found wherever it landed. Lines of that span which contain none
  of the changed words are left unmarked — rewrapping moves text between source
  lines, and banding them all would highlight the lines above and below the
  words that actually changed.
- **Deletions** have no place in the new PDF and anchor to the neighbouring line.
- Changes inside math, floats or `tikz` tend to map to the environment's opening
  line — the same weakness the click direction has.
- Only the most recent turn's changes are shown; older turns' line numbers have
  moved on.

### Moving the view

Any `file:line` in the chat is clickable — a selection you attached, a changed
file, a chip — and the PDF scrolls there with a brief highlight.

The agent can move the view too. It has one in-process tool,
`show_in_pdf(file, line, why)`, so you can ask for things it has to go and find:

> take me to the summary statistics table

It greps the source, locates the caption, calls the tool, and the page scrolls
under you:

```
⏵ Grep  summary statistics in .
  ⎿ sections/data.tex:33: \caption{Summary statistics for the estimation sample…}
⏵ mcp__texai__show_in_pdf  file: sections/data.tex, line: 26
  ⎿ Showing sections/data.tex:26 — the view moved to page 3.
```

The tool only scrolls; it cannot change anything.

### Two views, one stream

The panel has a **Chat** tab and a **Transcript** tab over the same underlying
stream of entries, so the transcript cannot show something the chat quietly
dropped. (A third tab, **Source**, is the editor — see below.)

- **Chat** — prose in full, everything else as one-line activity: which files
  were touched, whether the build passed, the diff, the revert button, the cost.
- **Transcript** — the session the way a terminal shows it: your prompt, every
  tool call with its arguments, results, thinking markers, build lines, and a
  per-turn rule with status, line counts and cost.

```
──── t0001 ────
› The user marked 1 passage while reading `main.pdf`: …

● I'll read the file around that line first.

⏵ Read  sections/model.tex
  ⎿ 1  \section{Model} (+68 more lines)

⏵ Edit  sections/model.tex
    old_string: labour. The elasticity
    new_string: labor. The elasticity
  ⎿ The file … has been updated successfully.

✓ success · 3 turns · 5.6s · $0.0425

⚙ build  Build succeeded.
──── t0001 applied · 1 file · +1/−1 · $0.0425 ────
```

Full output is kept only for failures — a successful `Read` would otherwise
paste the whole file into the transcript, which is exactly what a terminal does
*not* show you. SDK bookkeeping (init handshakes, token tallies) is filtered.

### Attaching a real terminal

The panel is not an emulator, and a real terminal is better at being one. Once a
turn has run, **Attach terminal** copies:

```bash
claude --resume <session-id>
```

Run that in your project directory and Claude Code picks up the *same*
conversation — full TUI, slash commands, everything. Drive one at a time: the
browser panel and a terminal both writing to one session will confuse both.

### Down to the word

SyncTeX only resolves a point to a *line* — it reports `Column:-1` for every
engine in practice. But the browser knows which word was under the cursor,
because PDF.js draws a text layer over the page, so texai sends that word (and
the words either side of it) and the server matches it back into the source:

```
Cmd-click "elasticity"  →  sections/model.tex:41:15
```

Matching has to cross the gap between what TeX renders and what the source says,
so each source line is projected into roughly what it renders as — dropping
comments and markup, collapsing `\emph{elasticity}` to `elasticity`, `caf\'e` to
`café`, `---` to a dash — while remembering which source column every character
came from. The words either side of the click are what tell the third "the" on a
line from the first, and the search reaches a few lines past the one SyncTeX
named, since a wrapped paragraph puts the word after the line its box started
on.

Not everything renders near where it is written. `\maketitle` typesets a title
pages away from the `\title` line, and a float caption lands wherever the float
lands, so SyncTeX points at the command rather than the words. When nothing
turns up nearby, the search widens to the whole file and accepts only an answer
that cannot be wrong: exactly one occurrence in it. Near the click, proximity
decides; far from it, uniqueness has to.

It also knows when it has lost. A word it cannot find, a word that is only a
substring of a longer one, two equally plausible spots on *different* lines, or
several distant candidates with nothing to choose between them all give no
column at all, and the reference falls back to the line exactly as before. A
wrong line is worse than a missing column.

The precision shows up in four places: the chip reads `file:line:column`, the
agent is told *At the word: "elasticity"* rather than just a line number,
Alt-click opens the editor with that word selected, and the pencil on any chip
does the same for a passage you marked earlier.

### Both directions

Cmd-click goes from the page to the source. Clicking in the **Source** tab goes
back: the PDF scrolls to centre on whatever that line produced and flashes the
zone, so you can see where you are without reading page numbers.

```
click sections/model.tex:40  →  page 2 centres, the paragraph flashes
```

That is `synctex view` rather than `synctex edit`, and it answers with the boxes
a line produced — so a paragraph that wraps flashes every rendered line of it,
and a line that produces nothing (a comment, `\begin{document}`, a macro
definition) does nothing at all rather than complaining.

It is bound to the click, not to the cursor, so typing and arrow keys never yank
the page around while you are writing. Rapid clicks only ever honour the newest,
so a slow answer cannot scroll you back to where you were two clicks ago.

### Editing it yourself

Not everything is worth a prompt. The **Source** tab is a LaTeX editor over the
same files the agent edits: **Alt-click** any passage in the PDF to open its
source at that line, type, and **Cmd/Ctrl-S** to save. Saving recompiles and the
page reloads where you were.

```
Alt-click "the elasticity of substitution"  →  Source tab, sections/model.tex:41
```

The `file:line` label on a change card opens the same editor at that change, for
when it is faster to fix the agent's work than to describe the fix.

Two writers on one tree needs a rule, so every save carries a hash of the text it
was based on. If the agent rewrote the file while it sat open, the save is
refused with *"changed since you opened it"* rather than discarding that work;
**Reload** takes their version. Going the other way, when a turn finishes, an
open file with no unsaved typing quietly picks up the agent's version.

Your own saves are not part of the review. They fold into the baseline, so the
Changes overlay keeps showing what the *agent* did — your typing never arrives as
a hunk waiting to be accepted.

A save that breaks the document reports the LaTeX errors under the editor and
marks the offending lines. The line numbers in a TeX log name no file, so a line
is only marked when the text the log quotes is really on it; the rest still show
in the message.

Only files a LaTeX build reads are editable — `.tex`, `.bib`, `.cls`, `.sty`,
`.bbx`, `.cbx`, `.lco` — and only inside `--root`.

### Committing

If the project is in a git repository, the toolbar carries a pill: the branch,
how many files are uncommitted, and how many commits are waiting to be pushed.

```
⑂ main  2  ↑1
```

Click it for the file list and three buttons. **Commit** asks the agent to read
the diff and write a message, then shows it in an editable box — nothing is
committed until you press Commit again, and you can rewrite the message first.
**Pull** is `git pull --rebase`; **Push** publishes, setting an upstream if the
branch has none and there is only one remote to mean.

Everything is scoped to `--root` with an explicit pathspec. If your paper is a
subdirectory of a bigger repository, the panel says so, and a commit made here
takes only files under the root — work you staged elsewhere in that repository
stays staged and uncommitted.

The refusals are as deliberate as the actions. A pull will not run over
uncommitted work, a commit will not run over unresolved conflicts, and network
operations run with prompting disabled: a repository that needs a password fails
in seconds with a message rather than hanging on a prompt in a terminal you
cannot see. Opening the panel fetches (at most once a minute) so "behind" is a
real number and not a stale one.

The commit message is a one-shot query with no tools — it cannot edit anything,
and it never enters the review conversation or the transcript. If the agent is
unavailable it falls back to `Update sections/model.tex, ...` and says so.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--root` | required | project root; everything stays inside it |
| `--pdf` | required | PDF to review, relative to `--root` or to your shell |
| `--build-cmd` | `latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>` | how to rebuild |
| `--build-dir` | the root `.tex` file's directory | where to run the build |
| `--model` | the CLI's default | model for the agent |
| `--port` | `8765` | port on `127.0.0.1`; the next free one is used if busy |
| `--synctex` | `synctex` | path to the `synctex` executable |
| `--open` | off | open the viewer in your browser |

`--build-cmd` is split with `shlex` and run without a shell, so `&&` and pipes
are not supported — point it at a script if you need them.

## Without the agent

Everything still works as a plain review tool. Every click is written to
`<project-root>/.texai/current-selection.json`, atomically, so an agent
running anywhere else can read it:

```json
{
  "version": 1,
  "updatedAt": "2026-08-13T18:20:31-05:00",
  "pdf": "build/main.pdf",
  "page": 7,
  "pdfPosition": {"x": 241.3, "y": 418.2},
  "source": {"file": "sections/model.tex", "line": 143, "column": 15, "word": "elasticity"},
  "selectedText": null
}
```

`pdfPosition` is in PDF points from the **top-left** corner of the page, the
convention SyncTeX uses. Paths are POSIX and relative to the project root.

`word` is the word the click landed on, and `column` points at it. When the word
could not be matched in the source with confidence, `word` is `null` and
`column` is `1` — read the line and ignore the column in that case. See
[Down to the word](#down-to-the-word).

There is also a **Copy reference** button:

```text
PDF selection: sections/model.tex:143
Rendered text: “the estimated elasticity”
```

`skills/review-latex-pdf/SKILL.md` teaches an external coding agent to drive
that flow.

## Requirements

- `uv` and Python ≥ 3.10.
- A TeX installation providing `synctex` and `latexmk`.
- A PDF compiled with SyncTeX enabled — a `.synctex.gz` next to it.
- The `claude` CLI, for the chat panel. The agent SDK itself installs with
  texai.

Missing pieces are reported specifically rather than as a generic failure: the
CLI warns at startup, and clicking or sending tells you exactly which piece is
absent.

## Safety

- Binds to `127.0.0.1` only and rejects non-loopback `Host` headers.
- `synctex` and the build command are invoked with argument lists and
  `shell=False`. Nothing from the browser reaches a shell.
- Every path — the PDF, whatever SyncTeX reports, every selection the browser
  sends back — is resolved through symlinks and must stay inside `--root`.
- The agent gets `Read`, `Write`, `Edit`, `Glob` and `Grep`. No `Bash`, no web
  access, no subagents. It cannot run the build; the harness does that.
- The agent gets one tool of our own, `show_in_pdf`, which scrolls your view
  and nothing else.
- Every turn is snapshotted before the agent starts, so any turn can be undone
  and a turn that breaks the build undoes itself. Snapshots are plain file
  copies in a temp directory keyed by project — deliberately *outside* the
  project, or they would turn up in the agent's own Glob and Grep results. No
  git history is written.

## Releasing

The version lives in one place, `src/texai/__init__.py`; `pyproject.toml` reads
it from there, so the two cannot drift.

```bash
# bump __version__, commit, then:
git tag v0.2.0 && git push origin v0.2.0
```

That runs `.github/workflows/release.yml`: tests on the oldest and newest
supported Python, a check that the tag matches `__version__`, a build, a check
that the wheel really contains the viewer and its vendored assets, `twine
check`, and then the upload. Any of those failing stops the release — which
matters, because a version once on PyPI can never be replaced.

Authentication is [PyPI Trusted
Publishing](https://docs.pypi.org/trusted-publishers/), so there is no API token
in the repository to leak or rotate. It needs one-time setup on PyPI: **your
project → Publishing → Add a new pending publisher**, with workflow
`release.yml` and environment `pypi`.

`workflow_dispatch` runs everything except the upload, which is the way to prove
the pipeline before committing to a version number.

## Third-party code

`src/texai/static/vendor/` holds pinned copies of two libraries, vendored so
nothing is fetched from a CDN at runtime:

- `pdfjs/` — [PDF.js](https://github.com/mozilla/pdf.js) 5.4.149 (Apache-2.0),
  which renders the document.
- `codemirror/` — [CodeMirror](https://codemirror.net/5/) 5.65.21 (MIT), which
  is the Source tab. Version 5 rather than 6 because it ships plain UMD files
  and this project has no build step.

Each licence travels with its directory. Everything else here is MIT — see
`LICENSE`.

## Development

```bash
uv sync
uv run pytest              # unit + orchestration tests
tests/browser/run.sh       # layout regression checks (needs node + Chrome)
```

The browser checks exist because every layout bug here has been invisible to
both the Python tests and to DOM-property assertions: a flex child missing
`min-height: 0` that pushed the page taller than the viewport, a `hidden`
attribute outranked by `display: flex`, and turn cards squashing to slivers
instead of scrolling. Only measured geometry catches those. They send no agent
turns, so they cost nothing to run.

```
src/texai/
  cli.py         argparse + uvicorn on 127.0.0.1
  config.py      validated, root-confined paths and build settings
  paths.py       containment and project-relative normalization
  synctex.py     `synctex edit` invocation + output parsing
  selection.py   selection payload + atomic JSON write
  models.py      request validation
  events.py      pub/sub bus + SSE framing with replay
  agent.py       the Claude Agent SDK session
  transcript.py  one entry stream, rendered as chat or as a terminal log
  prompt.py      passages + instructions -> a prompt
  build.py       compiling, and reading errors out of the log
  snapshots.py   per-turn source copies, diffing and revert
  hunks.py       structured diff hunks: stable ids, per-hunk rollback
  navigate.py    locating a source line in the rendered PDF
  turns.py       snapshot -> agent -> build -> diff, and the review session
  source.py      reading and writing source files for the editor
  words.py       matching a rendered word back to its source column
  git.py         root-scoped status, commit, pull --rebase, push
  commitmsg.py   the agent's one-shot commit message, with a fallback
  server.py      FastAPI routes
  static/        vanilla two-panel UI + vendored PDF.js and CodeMirror
                 (marks.js draws the inline change markers,
                  editor.js is the Source tab, git.js the git panel)
```

## Not built yet

- **`latexdiff` view.** The diff is textual (`.tex`). Compiling a marked-up
  "changes" PDF and toggling it in the viewer is the natural next step, with the
  textual diff as the fallback when `latexdiff` chokes on a document.
- **Streaming partial text.** Agent messages appear per block, not per token.
  The SDK supports finer streaming (`include_partial_messages`); the panel does
  not use it yet.
- **Highlights across hyphenation and rewrapping.** Markers are narrowed to the
  changed words by matching the diff against the PDF.js text layer. A word
  broken across a line, or one whose glyphs the layer splits oddly, still falls
  back to banding its line.
- **An embedded interactive terminal.** The Transcript tab is read-only. A real
  in-browser terminal (xterm.js over a PTY) would be a second agent process
  unless it took over the loop entirely — which would cost the snapshot, build
  and revert orchestration. `claude --resume` gets you the real thing instead.
