# texai

A canvas for LaTeX: read the compiled PDF on the left, talk to a coding agent on
the right. Cmd/Ctrl-click a passage to attach it to your message; the agent edits
the source, `texai` rebuilds the document, and the page reloads in place.

The bridge that makes it work is SyncTeX — a pixel on page 7 becomes
`sections/model.tex:143`, which is what the agent actually needs.

## Install

```bash
pip install "texai[agent]"   # viewer + SyncTeX bridge + the chat agent
pip install texai            # viewer + SyncTeX bridge only (no chat panel)
```

The `[agent]` extra pulls in the Claude Agent SDK; the plain install runs the
viewer and SyncTeX bridge and tells you what is missing if you open the panel.
See [Turning on the agent](#turning-on-the-agent) for the `claude` CLI it also
needs.

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

The chat panel needs the Claude Agent SDK and the `claude` CLI. Without them the
viewer, the SyncTeX bridge and the selection file all still work, and the panel
tells you what is missing.

```bash
uv sync --extra agent      # installs claude-agent-sdk
```

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

**Reject** rolls back that one change and leaves the rest of the turn alone, so
you can keep three edits out of four from a batch. It is a reconstruction, not a
patch: the file is rebuilt from the turn's snapshot with the rejected region
taken from the old text, then recompiled. Hunk ids are content-derived rather
than positional, so rejecting one does not renumber the others.

**Accept** just marks it reviewed. Nothing blocks — ignore the markers and keep
working if you would rather.

Where it is approximate, honestly:

- Highlights are **line-level**. SyncTeX reports the line box, which is the full
  column width, so a one-word change highlights its whole line.
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
dropped.

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
  "source": {"file": "sections/model.tex", "line": 143, "column": 1},
  "selectedText": null
}
```

`pdfPosition` is in PDF points from the **top-left** corner of the page, the
convention SyncTeX uses. Paths are POSIX and relative to the project root.
`column` is `1` when SyncTeX reports none, which is usually.

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
- For the chat panel: `claude-agent-sdk` and the `claude` CLI.

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

## Third-party code

`src/texai/static/vendor/pdfjs/` is a pinned copy of
[PDF.js](https://github.com/mozilla/pdf.js) 5.4.149 (Apache-2.0), vendored so
the viewer needs no CDN at runtime. Its licence travels with it in that
directory. Everything else here is MIT — see `LICENSE`.

## Development

```bash
uv sync --extra agent
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
  turns.py       snapshot -> agent -> build -> diff, or revert
  server.py      FastAPI routes
  static/        vanilla two-panel UI + vendored PDF.js
                 (marks.js draws the inline change markers)
```

## Not built yet

- **`latexdiff` view.** The diff is textual (`.tex`). Compiling a marked-up
  "changes" PDF and toggling it in the viewer is the natural next step, with the
  textual diff as the fallback when `latexdiff` chokes on a document.
- **Streaming partial text.** Agent messages appear per block, not per token.
  The SDK supports finer streaming (`include_partial_messages`); the panel does
  not use it yet.
- **Word-level highlights.** Markers cover the whole line, because that is what
  SyncTeX reports. Narrowing them means matching the diff text against the
  PDF.js text layer.
- **An embedded interactive terminal.** The Transcript tab is read-only. A real
  in-browser terminal (xterm.js over a PTY) would be a second agent process
  unless it took over the loop entirely — which would cost the snapshot, build
  and revert orchestration. `claude --resume` gets you the real thing instead.
