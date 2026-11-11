# texai

**Overleaf, but the collaborator is your coding agent — and none of it leaves
your machine.** The compiled PDF on the left, the agent on the right, working on
your actual `.tex` files, on your own disk, syncing to whatever git remote you
already use. No upload, no second copy of your project, no account.

The whole thing is built around one loop: you point at something on the page,
the agent edits the source, texai rebuilds the document, and every change comes
back marked on the page for you to accept or reject.

- **Point at the page and the agent knows exactly where you mean.** Select a
  passage or click a word and it becomes a reference in your next message —
  `sections/model.tex:143:15`, the precise word, not a description of roughly
  where you meant. Attach several, each with its own note. Or ask right there:
  select a sentence, click the ✦ that appears, type *tighten this*, and the
  edit comes back on the page — no chat panel needed. When there is more than
  one good way to say it, the agent offers versions side by side and you click
  the one you want.
- **Review its edits on the page, not in a diff.** Every change is marked inline
  in the PDF, old text struck through and new text beside it. Accept or reject
  them one at a time, across as many turns as you like.
- **The agent drives the view.** It can scroll you to the table it just changed,
  and you can ask it to — *take me to the summary statistics table*.
- **Its work is checked before it reaches you.** texai owns the build: after
  each turn it recompiles, hands any LaTeX errors back to the agent to fix, and
  reverts the whole turn if the document cannot be made to compile. You are
  never left with a broken paper.
- **It only touches your LaTeX.** The agent gets file tools and nothing else —
  no shell, no network. And `claude --resume` drops the same session into a real
  terminal when you want the full CLI.
- **Take over by hand whenever you like.** Double-click a word on the page to
  edit its paragraph right there, LaTeX and all. Alt-click opens the LaTeX at
  that spot in a full editor; Cmd/Ctrl-S saves and recompiles, and the page
  reloads where you were. Clicking in the source scrolls the PDF back to it.
- **Find your way around a long paper.** A contents column beside the page lists
  the whole document — every file it pulls in, its figures and tables by caption,
  its theorems by title — and clicking one scrolls the PDF there. Cmd/Ctrl-P
  jumps to any of them by name, and the paper's own `\ref` links work: click
  *Equation (1)* and the page goes to equation (1).
- **Annotate the paper in the paper.** `%@intent motivation` names what a
  paragraph is for; `%@comment`, `%@todo` or a bare `%@` leaves a note. They are
  ordinary LaTeX comments, so they travel with the source through git, and the
  preview shows them in the page margins beside the text they describe.
- **Know how far you have drifted from git.** The toolbar carries the branch,
  how many files are uncommitted, and how many commits you are ahead or behind
  the remote — scoped to your project, even when it is a subdirectory of a
  larger repository.
- **Commit without leaving the page.** The agent reads the diff and writes the
  message; you edit it if you like, then commit, pull or push.

The bridge that makes all of it work is SyncTeX — a pixel on page 7 becomes
`sections/model.tex:143`, which is what the agent actually needs. SyncTeX stops
at the line; matching the clicked word against the source gets it to
`sections/model.tex:143:15`.

## Quick start

One command on a paper you already have — nothing to install:

```bash
uvx texai /path/to/paper/main.tex --open
```

Name the `.tex`, or the PDF it builds, and texai works out the rest:

- **From a `.tex`**, the PDF is wherever `latexmk` will write it: next to the
  `.tex`, or under the `$out_dir` your `latexmkrc` names, or wherever an existing
  `main.pdf` with its SyncTeX data already sits below it.
- **From a PDF**, the source is the `.tex` with the same name, beside it or a few
  directories up — `build/main.pdf` is built from `../main.tex`.
- **From a directory, or from nothing at all** in the paper's own directory, it
  is the one `.tex` there that opens with `\documentclass` (`main.tex` if there
  are several).
- **The project root** is the narrowest directory holding both, which is usually
  the `.tex`'s own. Everything texai and the agent touch stays inside it.

The PDF does not have to exist yet: texai compiles it, then opens the viewer on
<http://127.0.0.1:8765/>. All it needs is a TeX installation with `latexmk` and
`synctex`. When a guess is wrong, `--root` and `--pdf` override it.

Nothing to hand? This repository's `example/` is a short paper split across four
files, and ships uncompiled on purpose:

```bash
git clone https://github.com/tlamadon/texai && cd texai
uvx texai example/main.tex --open
```

`uvx` is `uv tool run`: it fetches texai into a throwaway environment, runs it,
and leaves nothing behind. `uv run texai` is the other one — it runs texai from
the project you are standing in, which is what you want in a checkout of *this*
repo (`uv sync` first) and not what you want anywhere else.

### Turning on the agent

The Claude Agent SDK comes with texai, whichever way you run it. The one thing
it cannot bring along is the **`claude` CLI** that the SDK drives — if

```bash
claude --version
```

answers, the chat panel works. Without it the viewer, the SyncTeX bridge and the
selection file all still work, and the panel names the missing piece rather than
failing vaguely.

The agent runs as a normal local Claude Code session and uses whatever
credentials `claude` is already logged in with — your subscription, if that is
how you use Claude Code. No API key needed, and turns count against that
account exactly as they would in the terminal.

(The Agent SDK docs do restrict *redistributing* subscription login: shipping
this to other people who sign in with their own Claude accounts needs approval
from Anthropic, and API-key auth otherwise. That is about distribution, not
about running it yourself.)

### Using Codex instead

texai can drive OpenAI's Codex instead of Claude. Its SDK is an optional extra,
because it carries its own Codex runtime (a few hundred MB):

```bash
uvx 'texai[codex]' main.tex --agent codex
```

It uses whatever `codex login` already set up, ChatGPT sign-in or API key.
Everything else works the same: the chat panel, the inline changes, revert,
`show_in_pdf`, commit messages, switching conversations. **Copy terminal
command** gives `codex resume <id>` instead of `claude --resume`. `--model` is passed through, so `--model gpt-5.4`
picks the Codex model.

One thing is looser (see [Safety](#safety)): Codex has no files-only mode, so it
reads with shell commands and edits with patches, in Codex's own sandbox.

### Keeping it around

`uvx` re-resolves every so often. Once texai has earned a place on your PATH:

```bash
uv tool install texai        # then just: texai main.tex
pip install texai            # or the pip equivalent
```

Either way the agent SDK comes with it. The one piece neither can install is the
`claude` CLI above.

(`pip install "texai[agent]"` still works. The extra is empty now that the SDK
is a plain dependency, and is kept only so the 0.1.0 install line does not
break.)

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

### Asking from the page

For a request about one passage, there is a shorter way round. Select the text
in the PDF and a **✦** appears just above the end of the selection; click it, or
press **Cmd/Ctrl-K**, and a box opens under the passage. Type the request and
press **Enter**.

```
select "Page breaks are where a naive line-based mapping tends to go wrong"
  ✦  →  "Make this sentence shorter and punchier."  →  Enter
```

It is the same turn a chip with a note would send, so it shows in the chat as
usual, and it works with the panel put away. While the agent works the passage
pulses and the box shows what it is doing, with a **Stop** button. When it is
done:

- **It edited something** — the box closes and the change appears as a marker on
  the rebuilt page, ready to accept or reject, even if *Show changes* was off.
- **It offered versions** — the box widens and shows them side by side, the
  words each one changes highlighted. Click one, or press its number, and texai
  puts it in place itself — no second agent call — then rebuilds, and it comes
  back as a change marker like any other edit. Picking another one later swaps
  it in for the first.
- **It only answered** — *is this sentence clear?* — the answer stays in the box
  until **Esc**, with **Open in chat** for the whole turn.

The small **auto · best · ×3** switch in the box says how many versions you
want. *auto* leaves it to the agent, which offers versions only when a request
has more than one good answer (*a better title*, *tighten this*) and edits
directly otherwise; *best* asks for one edit, made directly; *×3* asks for three
versions to pick from. The switch keeps its setting from one request to the
next. The versions also stay in the turn's chat card, so you can still pick one
after closing the box.

Offering costs nothing: the file is untouched until you pick. To decide later
instead, **Keep as proposal** under the versions writes them into the source as
`%@propose` lines above the passage (see *Proposals*). From there they outlast
the session: a card in the margin opens them again tomorrow, or on a
co-author's machine.

Closing the box does not stop the agent; the turn finishes in the chat either
way. One request runs at a time, so the ✦ stays away while the agent is busy.

### What the console says

The browser has the whole story; the terminal you started texai in gets the
short version. A compile is the slowest thing here and the only one that starts
without you asking directly — after a turn, after a save — so each one says when
it began and how it went. Pressing Ctrl-C answers immediately, and says what is
holding the exit up rather than appearing to hang; pressing it again stops
waiting.

```
texai: building — your save…
texai: built in 1.8s
texai: building — the agent's edits…
texai: build failed in 1.2s (exit 12) — ! Undefined control sequence. (line 3)
texai: interrupt received — stopping (waiting: a build has been running for 4s). Ctrl-C again to stop at once.
texai: stopped.
```

Every line is flushed as it is written, because that output is usually a log
file: agents start texai with stdout redirected, and a block-buffered pipe would
hold the lot back until the process ended.

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

Its second tool, `offer_options(file, original, options)`, is how it hands you
versions to pick from (see *Asking from the page*). texai checks that
`original` occurs exactly once in the file, and the agent is told to fix its
quote if not.

The tool changes nothing in the file. Your pick is applied by texai as a plain
replacement of that text, in a turn of its own. If you keep the versions for
later instead, texai writes them above the passage as `%@propose` lines,
widened to whole lines; picking one then puts it in and takes those lines out.

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

### Which conversation

Each launch starts a new agent conversation unless you say otherwise, and the
chat header names the one the panel is talking to. Its menu lists every
conversation saved for this project and switches to any of them:

- **Earlier in texai** — the ones texai ran, by the first thing you asked,
  with how many turns each took. `texai --resume` picks up the most recent at
  launch; `texai --resume <id>` a particular one.
- **From Claude Code in a terminal** (or Codex) — conversations started outside
  texai in this directory. Switching takes one over: the agent carries on with
  everything it was told there.
- **Start a new conversation** — the agent forgets; the review does not.

A switch takes effect with the next message, and the chat marks where it
happened, with what was said there before folded underneath. Pending changes
are untouched either way: they are measured from the files, not from what a
conversation remembers. The one limit is that turns run outside texai left no
snapshots, so a conversation taken over from a terminal can be read and carried
on, but its earlier edits cannot be reverted from here.

texai remembers its own conversations in `.texai/sessions.json`. Its side calls
— the quick editor, commit messages — are not saved as conversations, so they
never clutter the list.

### Attaching a real terminal

The panel is not an emulator, and a real terminal is better at being one. Once a
turn has run, **Copy terminal command** in the conversation menu copies:

```bash
claude --resume <session-id>
```

Run that in your project directory and Claude Code picks up the *same*
conversation — full TUI, slash commands, everything. Drive one at a time: the
browser panel and a terminal both writing to one session will confuse both.
Coming back works too: pick the conversation from the menu and texai carries
on where the terminal left off.

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
back: the PDF scrolls to centre on the words you clicked and flashes them, so
you can see where you are without reading page numbers.

```
click sections/model.tex:40  →  page 2 centres, the phrase there flashes
```

That is `synctex view` rather than `synctex edit`. SyncTeX answers with the
whole rendered line, which is a wide thing to light up when you pointed at one
word in it, so the words around the click are matched against the rendered text
and the highlight narrows to the phrase itself:

```
click sections/model.tex:40 on "objective"
  →  "objective, and differentiating" lights up — 251px of an 851px line
```

Candidates are tried longest first, because a run of words is unmistakable when
it is there and simply absent when a line break, a ligature or a hyphenation ate
it — and then a shorter one still lands. Clicking on markup (`$\alpha$`, a macro
name) narrows to nothing, since none of those characters are on the page, and a
line that produces nothing at all (a comment, `\begin{document}`) does nothing
rather than complaining. Whenever the words cannot be found the whole line is
still highlighted, which is the honest answer rather than no answer.

It is bound to the click, not to the cursor, so typing and arrow keys never yank
the page around while you are writing. Rapid clicks only ever honour the newest,
so a slow answer cannot scroll you back to where you were two clicks ago.

### Scrolling together

Clicking is exact but deliberate: you ask, once, per passage. Reading with both
panes open means asking over and over, so **Sync scroll** in the View menu ties
them together instead. Scroll the page and the source arrives at whatever is on
screen; scroll the source and the page follows. Off by default, and remembered
between sessions.

```
Sync scroll on, page scrolled to 45%  →  sections/robustness.tex:27, page 3
```

Whichever pane you touch leads, and the other stops listening while it is being
moved — otherwise every answer arrives as the next question and the two chase
each other down the document. What was matched lands near the top of the view
rather than centred, in both directions, so a passage stays under the same part
of the window as you read on.

The mapping is the same `synctex` pair the clicks use, with two allowances for
the looser question. The line at the top of an editor is as often as not blank,
a comment or a `\begin`, so the lookup walks forward to the first line that
rendered something. The top of a page is often a margin or the gap above a
float, so the probe tries a few depths into the view. When neither finds
anything — a full-page figure, the preamble — nothing moves, which beats moving
somewhere wrong.

None of it touches your cursor: the source is scrolled, not typed in, and the
line the page is showing gets a thin rule in the margin rather than the focus. A
file with unsaved edits is never swapped out from under you, so a page showing
some other file simply leaves the buffer where it is.

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

For a small fix there is no need to leave the page: **double-click** a word and
an editor opens over its paragraph, set in a serif at the page's size and as
wide as its column, with that word selected. It holds the paragraph's LaTeX —
type `\emph{…}`, `$\alpha_t$` or `\cite{…}` as you would in the source.
**Cmd/Ctrl-Enter** or clicking elsewhere saves that one paragraph and
recompiles; **Esc** cancels. The box stays up until the rebuilt page replaces
the text under it, and if the build fails it stays open with LaTeX's error.

A paragraph is a blank-line-separated block, the same one coloured in the
gutter and the margin. Only that block is written back, and only if it is still
what the box was opened on, so the agent editing elsewhere in the same file
costs you nothing. If the Source tab holds unsaved edits to the same file, save
or revert those first.

With editing on the page, the chat and the source can be put away: the panel
button at the right of the toolbar (or **Cmd/Ctrl-\\**) hides them and gives the
page the whole window. Alt-clicking into the source or attaching a passage
brings the panel back. The coloured paragraph bars in the margin matter most
beside the source, so **Paragraph guides** in the View menu switches them off; your `%@`
notes stay.

When you are stuck mid-sentence, **Ctrl-Space** (or **Alt-\\**, for systems where
Ctrl-Space switches the keyboard layout) asks for a continuation at the caret.
It shows up as faded ghost text: **Tab** takes it, **Esc** or carrying on typing
drops it. Nothing is requested until you ask, so typing never waits on it. It
calls the Anthropic API directly rather than going through the agent, so it
needs `ANTHROPIC_API_KEY` (or an `ant auth login` profile); without one, the
⋮ menu leaves the item out.

### Annotations in the source

Notes about the paper belong in the paper. A comment line that opens with `%@`
is an annotation. TeX skips it like any other comment, and texai shows it
beside the page.

Intent, comments, provenance and edit proposals are all annotations, and all
are written the same way:

```latex
%@kind [key:value ...] [-- text]
%> more lines, when there is more to say than fits on one
```

The word glued to the `@` is the kind; a bare `%@ ...` is a comment. Any
annotation can carry four fields:

- **`id:c4`** names it, so that others can point at it.
- **`by:TL`** says who wrote it. The agent signs `by:agent`. Write
  `by:"Two words"` for a value with spaces.
- **`re:c4`** makes it a reply to `c4`.
- **`status:done`** marks it as settled: a todo done, a question answered.

The paper is written one sentence per line, and an annotation is about the line
below it, so in practice it is about one sentence.

```latex
%@intent motivation, contrast with prior work
We estimate the elasticity from the 2008 reform.
%@comment id:c1 by:TL Is this consistent with Table 2?
%@comment id:c2 re:c1 by:agent Yes, column (2) gives 0.31.
%@todo status:done -- cite the 2019 replication
We find a value close to one.
```

| Kind | What it is | Where it shows |
|---|---|---|
| `%@intent` | a few words naming what the paragraph is *for* | left margin, beside the paragraph's colour bar, in its colour |
| `%@comment`, `%@todo`, `%@question`, any other word | a note on the line below | a slip in the right margin, clamped to three lines until hovered |
| `%@src` | what backs the sentence below | a dotted underline on the sentence, and a mark in the margin (see *Provenance*) |
| `%@propose` | a version of the line below | a blue slip saying how many versions there are (see *Proposals*) |

**Notes, comments and todos**
- Fields come first and the rest of the line is the text, exactly as written.
  `%@comment Note: see Table 2 -- TL` keeps all of it, because `Note:` is not a
  field this kind takes.
- A reply shows **↳** and its author.
- A settled item is faded, and a done todo is struck through.

**Where an annotation belongs**
- It belongs to the blank-line-separated paragraph it sits in. A block of
  nothing but annotations, set off by blank lines, belongs to the next
  paragraph down.
- `%@intent` is about its paragraph. Every other kind is placed beside the
  first line below it that typesets something.

**Commenting from the page**
- Select a passage and a comment button appears beside the ✦. It opens a small
  box: write the comment and press Enter. It goes into the source as
  `%@comment id:c3 by:you …` on the line above the sentence, and the page
  rebuilds. No agent is involved, so it works even when the agent is off.
- Clicking a note in the margin opens its thread: the note, its replies and
  who wrote each. From there you can reply, resolve or reopen it, delete a
  reply or the whole thread (deleting asks twice), or open it in the source.
- Replies fold into the note they answer, which shows how many it has.
- These are your own edits, saved like a save in the editor: they rebuild the
  page and do not show up as agent changes waiting for review.
- Comments are signed with `TEXAI_AUTHOR` if set, else with your git
  `user.name`. A two-word name is shortened to its initials; a one-word name is
  used as it is.

**In the app**
- Alt-click a note to open the editor on its line; clicking an intent always
  does. In the editor, `%@` and `%>` lines are tinted so they stand out from
  ordinary comments.
- **Notes** in the View menu hides and shows notes and proposals, and **Sources**
  does the same for provenance icons. Each entry only appears when the paper
  has something for it.

**How it is built**
- The grammar and the list of kinds live in one place,
  `src/texai/annotations.py`.
- Each kind declares where it goes, and the page draws by that alone. A new
  kind is a line in that table.

The agent is taught this grammar. It keeps a sentence's annotations with the
sentence when it rewrites or moves it, writes to the stated intent, and leaves
annotations alone unless asked. These are all ordinary requests:

- *Add an intent to every paragraph in the model section*
- *Answer TL's comments*
- *Mark the todos you resolved as done*

### Proposals

A `%@propose` is one version of the lines below it. Its `%>` lines are the
version's text, in full, and its free text is a label. Consecutive proposals
with the same `id:` are one choice:

```latex
%@propose id:p1 by:agent -- shorter
%> We find an elasticity of 0.3.
%@propose id:p1 by:agent -- with the range
%> We find an elasticity of 0.3 (0.25 to 0.36 across specifications).
We find that the elasticity, across all of our specifications, is about 0.3.
```

- **What a choice replaces:** one line, the sentence directly below it, unless
  `lines:N` says more. Nothing may come between a choice and the lines it
  replaces.
- **On the page:** a blue slip in the margin says how many versions there are.
  Clicking it opens them in the **Peek** tab, next to the current text, with
  the words each version changes marked.
- **Picking or dismissing:** picking one puts it in place and removes the whole
  choice. **Dismiss** removes the choice and keeps the text. Either is a turn of
  its own, reviewed and undone like any other edit.
- **Where they come from:** **Keep as proposal** on versions the agent offered
  (see *Asking from the page*); the agent, when asked to *suggest, don't edit*;
  or you, by hand.
- **Why they live in the source:** a proposal survives a restart, goes through
  git to a co-author, and can wait until you decide.
- **The cost:** they are source edits. They show in the diff, and writing them
  rebuilds the document, because the extra lines move SyncTeX's line numbers.
  That is why an offer stays out of the file until you ask to keep it.

### Records: annotations beside the paper

A sentence's sources and comments can outgrow the comment lines above it: a
claim backed by five papers, each with a quote and a page, and a conversation
about it. So they can live in `annotations.jsonl`, next to the root `.tex`, and
the sentence carries only a marker naming its record:

```latex
We find a large elasticity. %#k3f9q
```
```json
{"id": "k3f9q", "sources": [{"type": "paper", "target": "card2018", "page": 14, "quote": "…", "sha": "1a2b3c4d"}],
 "comments": [{"id": "c1", "kind": "comment", "by": "TL", "text": "…", "replies": [{"id": "c2", "by": "agent", "text": "…"}]}]}
```

- **The marker anchors the record.** It is part of the sentence's line, so any
  edit carries it along: yours, the agent's, a merge.
- **Adding a marker moves no line.** It goes at the end of the line, after a
  space, so the PDF and SyncTeX stay valid. Comments kept this way are
  instant: adding, replying, resolving and deleting never rebuild.
- **A marker alone on its line** is about the next line of text, like the `%@`
  annotations.
- **The file:** one record a line, sorted by ID. IDs are random, so two people
  adding records on two branches do not collide, and git merges the file like
  prose.
- **Undo:** it is snapshotted with the source, so undoing a turn undoes its
  records too. You can open it in the Source tab.
- **What is kept there:** comments written from the page go into records.
  Intents and proposals stay inline.
- **Moving existing papers over:** **Move annotations to annotations.jsonl**,
  in the Source tab's ⋮ menu, moves every inline `%@src` and comment into
  records, one per sentence, and leaves a marker. Threads keep their replies
  and status.
- **Inline forms stay valid:** a `%@src` line written by hand still works
  beside the records.
- **Detached entries:** `/api/paragraphs` reports records no marker points at,
  and markers whose record is missing, under `detached`.

The agent works with records through texai rather than editing the file:

- **`annotations(file, line)`** lists what is attached to a passage. A marked
  passage also lists its sources and comments in the turn, under "Attached".
- **`add_source`** records what backs a sentence. It checks the source as it
  goes: the label exists, the file is there, and a quote is in the paper word
  for word. It fills in the page and the hash itself, and refuses a misquote
  with the closest match.
- **`comment`** leaves a comment or answers one.
- **When it rewrites a sentence,** it is told to keep the marker at its end.

### Provenance: what backs a sentence

A `%@src` annotation records where a claim comes from. Write the paper one
sentence per line and put the `%@src` on the line above the sentence it backs;
a sentence can have several. The first word is the target:

```latex
%@src #tab:estimates
Table~\ref{tab:estimates} shows that the estimate is not delicate.
%@src code/estimate.py:4-8 -- the regression behind alpha-hat
Estimating~\eqref{eq:growth} by least squares gives $\hat{\alpha} = 0.34$.
%@src @smith2019 p.2 "the capital share of income is remarkably stable" sha:e760a4e2
That is in line with the cross-country evidence~\cite{smith2019}.
```

- **`#label`** is something in this paper, by its `\label`.
- **A path** is a file in the project, optionally with `:line` or
  `:start-end`.
- **`@key`** is a paper: `papers/<key>.pdf`, named after its bib key.

After the target, in any order:

- `p.N` is a page, in the PDF file's own count rather than the journal's
  printed page numbers.
- `"..."` is the passage that backs the claim, copied word for word.
- `sha:` gives the first hex digits of the PDF's SHA-256.
- `-- ...` is a note.

Each sourced sentence gets a faint dotted underline from its first word to its
last, and a small mark in the right margin, level with the line it ends on.

- **How the underline is placed:** SyncTeX only roughly says which words a
  source line produced, so the build gives the rough place, and the page finds
  the sentence's own words in its text there.
  - Math and references in the sentence are matched as gaps.
  - A trailing citation runs on to the sentence's full stop.
  - Where the words cannot be found, the underline falls back to the rough
    place.
- **Pointing at a mark** turns its sentence's underline solid and tints it.
- **Clicking a mark** opens the source in the **Peek** tab beside the page, or,
  for a sentence with several sources, lists them first. Each entry in the list
  shows what it links to (the paper's title and authors from your `.bib`, a
  file and its lines, or a label in this paper) and its quote or note.
- **Alt-click** opens the annotation in the editor.
- **Sources** in the View menu hides and shows the underlines and marks.

- **A paper or a project PDF** opens in a viewer of its own. The page you are
  reading stays where it was.
  - With a quote, texai searches the paper's text for those words and
    highlights them. The page number only says where to look first, since pages
    move between versions of a paper and words do not.
  - The search ignores spacing, hyphenation, accents and ligatures, and falls
    back to a close match.
  - The panel tells you when the quote is on a different page from the one
    written.
- **A label** opens this paper's own PDF with the labelled table, figure or
  equation outlined. **Go there** moves the main page to it.
- **A text file** opens with the named lines highlighted.
- **The hash** finds the paper even if the file is renamed. If the file in
  `papers/` has changed since it was quoted, it still opens, with a warning.

The bib entry, from any `.bib` in the project, supplies the title and authors
shown in the Peek header.

The agent knows the syntax, and reads papers with a tool of texai's own,
`read_paper`. Neither agent can open a PDF by itself: Claude has file tools
only, and its Read cannot open most papers; Codex has a shell but nothing in it
that reads PDFs. `read_paper` extracts the text with pypdf, so it needs nothing
installed on the system. Given a paper and a passage, it returns:

- the page, in the file's own count;
- the passage as the PDF actually words it;
- a `%@src` line, hash included, ready to paste.

A quote checked this way is one Peek will find when you click.

*Source the claims in this paragraph* has the agent read the papers and write
`%@src` lines with exact quotes, pages and hashes. It is told to say so when
nothing supports a claim, rather than cite loosely. *Check the sources in the
results section* has it read each one and report sentences their source does
not support.

### Finding your way around the source

Beside the editor is the document's outline: every `\section` and its relatives,
the figures and tables under their captions, theorem-like statements under their
titles, beamer frames, and the `\input` lines that pull the rest of the paper in.
Clicking a heading goes there in the source *and* scrolls the PDF to it; clicking
an `\input` opens the file it names. Whatever the cursor is inside is marked as
it moves.

```
Results
  Point estimates
    Table: Growth-accounting regressions. Standard errors…
  Transition dynamics
```

Floats are named by their `\caption`, wherever it sits in the environment —
usually under fifty lines of `tabular`. Statements are named by their
`\begin{theorem}[optional title]`, or failing that by their `\label`.
`\newtheorem` declarations are read as they are met, so `\begin{ass}` shows up
as *Assumption* once the file declaring it has been read; the usual names
(`theorem`, `lemma`, `prop`, `defn`, `assumption`, …) work without any
declaration. Equations and `proof` blocks are left out — there are too many of
them to be a map.

It is read from the buffer rather than from the file on disk, so a heading you
typed a second ago is already in the list, at the line it is really on. `≡` in
the editor bar folds the column away when the pane is narrow.

### Contents, beside the page

`≡` in the toolbar opens the same outline for the document you are *reading*:
the root `.tex` with every `\input` expanded where it appears, so a paper split
across four files reads as one. Clicking a heading scrolls the PDF to it —
nothing about the panel on the right changes, because you asked to read
something, not to edit it. **Alt-click** opens its source instead, the same
modifier that goes from page to source everywhere else.

```
Introduction
Model                       ← main.tex \input sections/model.tex
  Households
  Steady state
Robustness
Data
  Table: Summary statistics for the estim…
```

This one is read from the files on disk rather than from the buffer, and
deliberately: it is a map of the PDF, and the PDF was built from disk. Depth is
worked out across the whole document, so a chapter file that starts at
`\section` still sits under the `\chapter` that pulled it in.

### Go to anything

**Cmd/Ctrl-P** searches every heading in the project, and every file, from
wherever you are:

```
Cmd-P, "housh"  →  Households                              sections/model.tex:17
Cmd-P, "summ"   →  Table: Summary statistics for the esti… sections/data.tex:20
Cmd-P, "robu"   →  sections/robustness.tex
```

Picking a heading **moves the PDF**. The editor follows into the same place only
if the Source tab is already showing, so a jump never changes which tab you are
looking at. Picking a *file* opens it in the editor, since a file is a place in
the source and nowhere in particular in the PDF; so does picking a line that
produced no output in the PDF at all, which says so as it happens.

The match is fuzzy — the letters need only appear in order — and headings in the
file you are editing come first. The rest of the project is read from disk the
first time you ask, shared with the contents panel, and re-read whenever you or
the agent writes.

### The document's own links

A paper that loads `hyperref` typesets every `\ref`, `\eqref`, `\cite` and
`\url` as a link, and here they work: clicking one moves the page to what it
points at, near the top of the view rather than centred, because you are being
taken there to read on. The spot flashes on arrival, as it does after any other
jump.

```
click "Equation (1)"        →  the page moves to equation (1) and flashes it
click "Section 4"           →  page 4, at the heading
click a URL                 →  a new tab (http, https and mailto only)
```

Cmd/Ctrl-click and Alt-click still belong to texai even when they land on a
link, so attaching a passage and opening its source work over a `\ref` exactly
as they do over ordinary text.

Without `hyperref` a PDF carries no links at all, and nothing here can invent
them; `\usepackage[hidelinks]{hyperref}` gets you the links without the coloured
boxes, which is what the example does.

### Back, forward, and taking the PDF with you

The two arrows beside the page number walk the places the document has taken
you. Follow a `\ref`, click a heading in the contents, jump from the palette or
from a click in the source, and **back** returns to where you were reading.
Scrolling by hand is not a jump and puts nothing in the list — it only changes
where back will lead, which is what a reader means by it. **Forward** retraces,
and goes quiet the moment you jump somewhere new, as it does in a browser. Going
back restores the place and not the magnification: a zoom you chose since is
yours to keep.

The **⤓** beside the file name downloads the PDF as it currently stands — the
build on screen, not whatever was on disk when the page was opened. It is an
ordinary link, so right-click and *Save link as* works as well as clicking it.

Two writers on one tree needs a rule, so every save carries a hash of the text it
was based on — and when the file has moved underneath, the three versions are
merged rather than one of them being refused: the text the buffer was loaded
from, the buffer as it stands, and what is on disk now. The other writer is
nearly always in a different paragraph, and then its version simply arrives,
your unsaved typing untouched and your cursor where you left it. The lines that
came in are tinted green and the editor bar shows how many; clicking that count
walks through them. Both clear at your next save.

Only edits to the *same* lines are a real conflict. Then nothing is applied, the
buffer is left exactly as it was, and the editor says which file and what to do:
**Reload from disk**, under the **⋮** beside Save, takes their version. It is the
one action here that can discard an unsaved edit, which is why it sits behind a
menu rather than a click away from Save. The merge runs when a turn finishes,
when the PDF is rebuilt by anything at all — latexmk in a terminal, an agent
outside this app — and when you come back to the Source tab; a save that loses
the race merges and tries once more, so the usual case never reaches you as an
error at all.

Either way you keep your place. A merge splices only the lines that changed, so
the cursor holds its spot in the text and the line you are reading stays where
it is on screen. Re-reading a file outright — *Reload from disk* — puts the
cursor and the scroll back rather than at the top, and a browser refresh comes
back to the file you had open, at the line you were on, remembered per project.
That last one is only as good as the file still being roughly what it was: a
paragraph inserted above has moved everything down, and a remembered line number
cannot know by how much.

Your own saves are not part of the review. They fold into the baseline, so the
Changes overlay keeps showing what the *agent* did — your typing never arrives as
a hunk waiting to be accepted.

A save that breaks the document reports the LaTeX errors under the editor and
marks the offending lines. The line numbers in a TeX log name no file, so a line
is only marked when the text the log quotes is really on it; the rest still show
in the message.

Only files a LaTeX build reads are editable — `.tex`, `.bib`, `.cls`, `.sty`,
`.bbx`, `.cbx`, `.lco` — and only inside `--root`.

### Slides

A beamer deck opens like a paper: `texai talk.tex`. What changes is how you
point at it. Beamer collects a frame's body before typesetting it, so SyncTeX
records everything on a slide against the one line of its `\end{frame}`; a
click on a slide can say which slide and nothing finer.

So on slides texai goes the other way round. It reads each frame out of the
source, with the pieces a reader would point at: its title, each `\item`, each
paragraph of plain text. It then finds each piece's words on the page and hangs
a small icon beside them, plus a square one in the corner for the slide as a
whole.

- **Click an icon** and three buttons appear beside it, with the block lit up:
  ✦ to ask for a change, the bubble to leave a comment, the pencil to edit it
  yourself. A request reaches the agent as an exact range of lines
  (`talk.tex:19-22`, an item with its sub-list) on a named slide. "Add a bullet
  about X after this", "cut this slide to three points" and "tighten this" are
  single quick edits.
- **Edit it in place.** The pencil, or a double-click on the text, opens the
  block's LaTeX right there on the slide: one item, the title, a paragraph, or
  the whole frame from the slide's own icon. Saving rebuilds. Type a second
  `\item` under the first to add a bullet; empty the box to remove one.
  A paragraph that opens an environment comes with all of it, so the box on a
  `\begin{block}{Main point}` holds its `\end{block}` too, and a save that
  would leave a `\begin` without its `\end` is refused with the reason.
- **Or pick, as a browser's inspector does.** *Pick* in the toolbar turns the
  pointer into a selector: the block under it lights up (the innermost one: a
  sub-item, not the item around it; the slide's outline where there is no
  block) and a click picks it, with the same three buttons at the pointer. It
  stays on until you click *Pick* again or press Esc. With it, the icons are
  optional: *View › Slide anchors* takes them off the page.
- **Cmd/Ctrl-click an icon** to put the block on the composer as a passage;
  **Alt-click** to open it in the Source tab.
- **Hover an icon** to see what it stands for before asking anything of it.
- **Selecting or Cmd/Ctrl-clicking text** on a slide resolves to the block it
  is in, and a selection still travels as the words you selected.
- **A comment** goes above its block in the source, or above `\begin{frame}`
  when it is about the whole slide.

An icon always stands for known lines; the text matching only decides where to
draw it. A bullet with no words to find, one that is all symbols, takes the
unclaimed line between its neighbours. What is left without an icon is a block
that put no text on the page at all (a figure), or an item not yet uncovered on
an earlier overlay page; the slide's own icon still reaches it. A click on a
line that belongs to no block means the slide, never the line next to it.
Frames are found as
`\begin{frame}…\end{frame}` and `\frame{…}`; a custom frame environment is not
recognised.

Not there yet on slides: change markers, margin notes and "take me to" are not
placed inside a slide. `example-slides/talk.tex` is a deck to try it on.

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
| *document* | the document in the current directory | the `.tex`, its PDF, or a directory holding it |
| `--root` | the directory holding the `.tex` and the PDF | project root; everything stays inside it |
| `--pdf` | where `latexmk` writes it | PDF to review, relative to `--root` or to your shell |
| `--build-cmd` | `latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>`, with the engine the document needs | how to rebuild |
| `--build-dir` | the root `.tex` file's directory | where to run the build |
| `--agent` | `claude` | `claude` or `codex` (needs `texai[codex]`) |
| `--model` | the agent's default | model for the agent; also on the composer, where it can be changed mid-conversation |
| `--no-update-check` | off | do not ask PyPI whether a newer texai is out (otherwise a green arrow beside the name says so) |
| `--resume` [*id*] | a new conversation | carry on the last conversation texai had here, or the one given |
| `--port` | `8765` | port on `127.0.0.1`; the next free one is used if busy |
| `--synctex` | `synctex` | path to the `synctex` executable |
| `--open` | off | open the viewer in your browser |

The default build uses pdfLaTeX unless the document needs something else:
`-lualatex` or `-xelatex` in place of `-pdf` when a `% !TEX program = …` line
asks for it, when a `latexmkrc` beside it sets `$pdf_mode`, or when the root
file or a local class, package or `\input` it loads uses `fontspec`,
`unicode-math`, `polyglossia` or an engine-specific package. The startup banner
says which, and why.

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

The View menu also has **Copy reference to last selection**:

```text
PDF selection: sections/model.tex:143
Rendered text: “the estimated elasticity”
```

`skills/review-latex-pdf/SKILL.md` teaches an external coding agent to drive
that flow.

## Requirements

- `uv` and Python ≥ 3.10.
- A TeX installation providing `synctex` and `latexmk`.
- A PDF compiled with SyncTeX enabled — a `.synctex.gz` next to it. texai builds
  one at startup if it is not there.
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
- Claude gets `Read`, `Write`, `Edit`, `Glob` and `Grep`. No `Bash`, no web
  access, no subagents. It cannot run the build; the harness does that.
- Codex is looser, because it has no files-only mode: it runs shell commands
  and applies patches. It runs in Codex's `workspace-write` sandbox, so it can
  write only inside the project (and temp directories) and has no network, and
  with approvals denied, so it never stops to ask. It is told not to compile,
  but nothing prevents it.
- The agent gets six tools of our own:
  - `show_in_pdf` scrolls your view.
  - `offer_options` shows you versions of a passage to pick from.
  - `read_paper` reads a PDF inside the project as text. It reads only PDFs, and
    applies the same path checks as everything else.
  - `annotations` reads what is attached to a passage.
  - `add_source` and `comment` write to `annotations.jsonl` and put a marker on
    the sentence, inside the project, within the turn, so the turn's snapshot
    can undo them.

  The first four write nothing.

  Claude calls them in-process. Codex calls them over MCP on texai's own
  loopback port, which only exists when Codex is the agent.
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

`src/texai/static/vendor/` holds pinned copies of three libraries, vendored so
nothing is fetched from a CDN at runtime:

- `pdfjs/` — [PDF.js](https://github.com/mozilla/pdf.js) 5.4.149 (Apache-2.0),
  which renders the document.
- `codemirror/` — [CodeMirror](https://codemirror.net/5/) 5.65.21 (MIT), which
  is the Source tab. Version 5 rather than 6 because it ships plain UMD files
  and this project has no build step.
- `katex/` — [KaTeX](https://katex.org) 0.19.1 (MIT), which typesets the math
  in the agent's replies. Its ES module build, stylesheet and woff2 fonts only;
  the module loads the first time a reply has math.

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
  cli.py         argparse, the first build, and uvicorn on 127.0.0.1
  config.py      validated, root-confined paths and build settings
  paths.py       containment and project-relative normalization
  synctex.py     `synctex edit` invocation + output parsing
  selection.py   selection payload + atomic JSON write
  models.py      request validation
  events.py      pub/sub bus + SSE framing with replay
  agent.py       the Claude Agent SDK session
  sessions.py    which conversations exist here, and texai's record of its own
  transcript.py  one entry stream, rendered as chat or as a terminal log
  prompt.py      passages + instructions -> a prompt
  build.py       compiling, and reading errors out of the log
  snapshots.py   per-turn source copies, diffing and revert
  hunks.py       structured diff hunks: stable ids, per-hunk rollback
  navigate.py    locating a source line in the rendered PDF
  turns.py       snapshot -> agent -> build -> diff, and the review session
  source.py      reading and writing source files for the editor
  merge.py       three-way merge, so an outside edit and an unsaved one both live
  words.py       matching a rendered word back to its source column
  frames.py      a beamer deck's frames and their blocks, read from the source
  slides.py      which page is which frame, for the icons on each slide
  git.py         root-scoped status, commit, pull --rebase, push
  commitmsg.py   the agent's one-shot commit message, with a fallback
  server.py      FastAPI routes
  static/        vanilla two-panel UI + vendored PDF.js, CodeMirror and KaTeX
                 (marks.js draws the inline change markers,
                  history.js is the back/forward list of places,
                  editor.js is the Source tab, outline.js parses and draws
                  both tables of contents, project.js assembles the document
                  from its \input tree, jump.js is the go-to palette,
                  sync.js keeps the two panes on the same passage,
                  slides.js places the selector icons on beamer slides,
                  sessions.js the conversation menu, and git.js the git panel)
```

## Not built yet

- **Plan mode.** For a change worth arguing about before it happens: the agent
  writes a plan as markdown, you edit it in the same editor as the `.tex`, and
  only then does it touch the document. Snapshot, build and revert stay exactly
  as they are; what is new is a turn whose output is a file to read rather than
  edits to accept, and a way of saying "now do it" against a plan you may have
  rewritten in the meantime.
- **Learning how you write.** Every turn already records a choice: which of
  the offered versions you picked, which changes you kept and which you
  reverted, what you rewrote by hand right after the agent touched a line.
  Repeated enough, those choices are a style ("keeps the first person", "never
  *utilize*", "cuts the hedge, keeps the citation"). The idea is to log them
  locally, have the agent periodically consolidate them into a style file — a
  skill the agent reads on every turn, in your project or your home directory —
  and let you read and edit that file like any other source. Nothing leaves the
  machine beyond what a turn already sends.
- **The rest of the page on slides.** A beamer slide has icons to point with
  (see *Slides*), which gives a map from each block of the source to where it
  sits on the page. Change markers, margin notes and navigation could hang off
  the same map. A slide-at-a-time view, and telling the agent when a slide
  overflows, belong here too.
- **Selecting without a mouse.** Asking about a passage starts from a text
  selection, which is fiddly on a touchscreen and imprecise across lines. Two
  additions: a small icon beside each paragraph (the guide bars already know
  where every paragraph sits on the page) that opens a comment or a question
  scoped to it; and a way of picking whole lines — tap a first and a last line
  in the margin — rather than dragging across glyphs. Together they should make
  the page usable from a phone or a tablet.
- **A better speech interface.** Talking is often the quickest way to say what
  is wrong with a paragraph. Today that means the operating system's dictation
  into the request box. Something built in — hold to talk, transcription into
  the request, ideally pointed at the paragraph you are looking at or have
  tapped — would fit the reading-and-reacting loop, and the mobile one above
  especially.
- **A container.** `docker run` with TeX Live inside, so the one real dependency
  — a TeX installation carrying `latexmk` and `synctex` — stops being something
  to install first. Two things to work out: the agent needs the `claude` CLI and
  the credentials it is logged in with, which live on the host; and the whole
  point is that texai edits *your* files in place, so the project has to be a
  bind mount whose writes land as you rather than as root.
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
