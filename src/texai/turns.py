"""Turn orchestration: snapshot -> agent -> build -> diff, or revert.

A turn is the unit the UI shows and the unit you can undo. The sequence is
deliberately fixed rather than left to the agent:

1. snapshot the source tree
2. let the agent edit (or, when the user picks one of the versions the agent
   offered, put that version in place — no agent needed)
3. if nothing changed, stop here (a question, not an edit)
4. build; on failure hand the errors back to the agent and rebuild
5. still broken after the retries -> restore the snapshot, so a turn never
   leaves the document in a state that does not compile
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from .agent import AgentSession, AgentUnavailable
from .build import BuildError, BuildResult, run_build
from .config import AppConfig
from .events import EventBus
from .models import SelectionRef
from .hunks import reconstruct
from .options import OfferError, OfferStale, apply_option, offer_options
from .prompt import compose_build_failure_prompt, compose_turn_prompt
from .quick import (
    QuickSession,
    QuickStale,
    apply_edit,
    compose_quick_prompt,
    find_passage,
    parse_answer,
    quick_eligible,
)
from .selection import atomic_write_text
from .console import note
from .timing import Stopwatch, record, seconds
from .snapshots import Snapshot, diff_against, hunks_against, read_pair, restore, take_snapshot
from .transcript import build_entry, notice_entry, text_entry, user_entry

__all__ = ["Turn", "TurnController", "TurnBusy", "OfferError"]

MAX_BUILD_ATTEMPTS = 3
KEEP_SNAPSHOTS = 20
BASELINE_ID = "baseline"


class TurnBusy(RuntimeError):
    """A turn is already running; the UI serialises submissions."""


@dataclass
class Turn:
    id: str
    created_at: str
    message: str
    prompt: str
    selections: list[dict[str, Any]]
    status: str = "running"  # running | applied | answered | reverted | failed
    agent_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Versions of a passage the agent offered instead of editing it, each with
    # the index of the one picked once the user has (see texai.options).
    offers: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    build_ok: bool | None = None
    build_errors: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    hunks: list[dict[str, Any]] = field(default_factory=list)
    accepted_hunks: list[str] = field(default_factory=list)
    # (file, first line, last line) per hunk, so session changes can be traced
    # back to the turn that made them even after later turns renumber things.
    ranges: list[tuple[str, int, int]] = field(default_factory=list)
    review: str = "pending"  # pending | accepted | rejected | mixed
    cost_usd: float | None = None
    error: str | None = None
    reverted: bool = False
    # The id of this turn's turn_started event: where its live entries begin
    # in the event backlog, while the turn is still running.
    event_id: int = 0
    # Where the turn's time went, phase by phase (see texai.timing).
    timing: dict[str, Any] = field(default_factory=dict)
    watch: Stopwatch = field(default_factory=Stopwatch, repr=False)

    def as_dict(self, *, include_diff: bool = False) -> dict[str, Any]:
        changes = [
            change if include_diff else {k: v for k, v in change.items() if k != "diff"}
            for change in self.changes
        ]
        return {
            "id": self.id,
            "createdAt": self.created_at,
            "message": self.message,
            "prompt": self.prompt,
            "selections": self.selections,
            "status": self.status,
            "agentText": self.agent_text,
            "toolCalls": self.tool_calls,
            "offers": self.offers,
            "changes": changes,
            "buildOk": self.build_ok,
            "buildErrors": self.build_errors,
            "transcript": self.transcript,
            "hunks": self.hunks,
            "acceptedHunks": self.accepted_hunks,
            "review": self.review,
            "costUsd": self.cost_usd,
            "error": self.error,
            "reverted": self.reverted,
            "timing": self.timing,
        }


class TurnController:
    """Runs turns one at a time and keeps their history."""

    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        agent: AgentSession,
        quick: QuickSession | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.agent = agent
        # The quick editor for one-passage requests (see texai.quick), if any.
        self.quick = quick
        self.turns: list[Turn] = []
        self._snapshots: dict[str, Snapshot] = {}
        # Everything changed since here is "pending review", however many turns
        # it took. Per-turn snapshots still back the whole-turn Revert.
        self._baseline: Snapshot | None = None
        self._accepted: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._counter = 0
        # What the agent should hear at the start of its next turn: picks
        # applied without it, so it knows which of its versions went in.
        self._news: list[str] = []

    def _emit(self, turn: Turn, entry: dict[str, Any]) -> None:
        """Record a harness-side entry so the transcript is complete, not just agent output."""
        turn.transcript.append(entry)
        self.bus.publish("agent_entry", turnId=turn.id, entry=entry)

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def history(self) -> tuple[list[dict[str, Any]], int]:
        """Every turn as the chat panel draws it, and the event id that covers.

        A page that loads this and then streams events after that id sees each
        thing once. Without the id it would replay the whole backlog on top of
        the history: every entry drawn twice, every old ``navigate`` scrolling
        the page again.

        A running turn's record is only partly written — the agent's entries
        join it when the agent finishes — so its transcript is rebuilt from the
        backlog, where every entry was published as it happened. Should the turn
        have outlived the backlog, the partial record is the best there is.
        """
        last = self.bus.last_id
        turns: list[dict[str, Any]] = []
        for turn in self.turns:
            record = turn.as_dict()
            if turn.status == "running" and turn.event_id:
                events = self.bus.since(turn.event_id - 1)
                if events and events[0].id == turn.event_id:
                    record["transcript"] = [
                        event.data["entry"]
                        for event in events
                        if event.type == "agent_entry" and event.data.get("turnId") == turn.id
                    ]
            turns.append(record)
        return turns, last

    def get(self, turn_id: str) -> Turn | None:
        return next((turn for turn in self.turns if turn.id == turn_id), None)

    def _new_turn_id(self) -> str:
        self._counter += 1
        return f"t{self._counter:04d}"

    # ---------------------------------------------------------------- running

    async def submit(self, message: str, selections: list[SelectionRef]) -> Turn:
        """Queue a composer submission. Returns immediately; work continues in the background."""
        if self.busy:
            raise TurnBusy("an agent turn is already running")
        phases: dict[str, int] = {}
        watch = Stopwatch()
        passage = None
        if self.quick is not None and quick_eligible(message, selections):
            passage = find_passage(self.config, selections[0])
        if passage is None and not self.agent.running:
            await self.agent.start()  # raises AgentUnavailable with a usable message
            phases["agentStartMs"] = watch.lap()

        if self._baseline is None:
            watch.lap()
            self._baseline = take_snapshot(
                self.config.root, self.config.snapshots_dir, BASELINE_ID
            )
            phases["baselineMs"] = watch.lap()

        prompt = compose_turn_prompt(message, selections, self.config.pdf_rel)
        if passage is None:
            prompt = self._with_news(prompt)
        turn = self._start_turn(message, prompt, [s.model_dump() for s in selections])
        turn.watch = watch  # the turn's clock starts with the agent start-up
        turn.timing["phases"].update(phases)
        if passage is not None:
            selection = selections[0]

            async def quick_edit(turn: Turn) -> None:
                await self._ask_quick(turn, passage, selection)

            turn.timing["route"] = "quick"
            self._task = asyncio.create_task(self._run(turn, quick_edit, phase="quickMs"))
        else:
            turn.timing["route"] = "agent"
            self._task = asyncio.create_task(self._run(turn, self._ask_agent))
        return turn

    def _with_news(self, prompt: str) -> str:
        """The prompt, after whatever the agent has missed since its last turn."""
        if not self._news:
            return prompt
        prompt = "\n".join(self._news) + "\n\n" + prompt
        self._news = []
        return prompt

    async def _ensure_agent(self, turn: Turn) -> None:
        """Start the agent mid-turn, for a quick edit that needs it after all."""
        if self.agent.running:
            return
        watch = Stopwatch()
        await self.agent.start()
        turn.timing["phases"]["agentStartMs"] = watch.total()

    async def _ask_quick(self, turn: Turn, passage: Any, selection: SelectionRef) -> None:
        """One passage, one request: the quick editor answers, texai applies it.

        Whatever it cannot do, or answers in a way texai cannot use, goes to
        the agent as an ordinary turn.
        """
        assert self.quick is not None
        outcome = await self.quick.run_turn(compose_quick_prompt(passage, selection), turn.id)
        if outcome.timing:
            turn.timing["agentCalls"].append({**outcome.timing, "quick": True})
        turn.tool_calls = list(outcome.tool_calls)
        turn.transcript.extend(outcome.entries)
        self._add_cost(turn, outcome.cost_usd)

        answer = parse_answer(outcome.text) if not outcome.error else None
        where = f"{passage.file}:{passage.start}"
        request = (selection.instruction or "").strip()
        if answer is not None and answer.kind == "edit":
            try:
                apply_edit(self.config, passage, answer.text)
            except QuickStale as exc:
                answer.kind, answer.text = "agent", str(exc)
            else:
                turn.agent_text = answer.note or f"Edited {where}."
                self._emit(turn, text_entry(turn.agent_text))
                self._news.append(
                    f"(Since your last turn: on the user's request “{request}”, texai's "
                    f"quick editor rewrote {passage.file}:{passage.start}-{passage.end}.)"
                )
                return
        if answer is not None and answer.kind == "versions":
            reply = await offer_options(
                self.config, self.bus, passage.file, passage.text, answer.versions
            )
            turn.offers = self._offers_since(turn)
            if turn.offers:
                turn.agent_text = f"{len(answer.versions)} versions of {where} to pick from."
                self._emit(turn, text_entry(turn.agent_text))
                return
            answer.kind, answer.text = "agent", reply
        if answer is not None and answer.kind == "reply":
            turn.agent_text = answer.text
            self._emit(turn, text_entry(answer.text))
            return

        why = answer.text if answer is not None else (outcome.error or "it did not answer")
        self._emit(turn, notice_entry(f"Handing this to the agent: {why}"))
        turn.timing["route"] = "quick→agent"
        await self._ensure_agent(turn)
        await self._ask_agent(turn, self._with_news(turn.prompt))

    async def pick(self, turn_id: str, offer_index: int, option_index: int) -> Turn:
        """Put one of the versions a turn offered in place, as a turn of its own.

        Its own turn so that it is reviewed, reverted and attributed like any
        other edit. texai makes the replacement itself; only if the passage has
        changed since the offer does the agent get asked to apply the version.
        Picking again swaps the new version in for the one picked before.
        """
        if self.busy:
            raise TurnBusy("an agent turn is already running")
        source = self.get(turn_id)
        if source is None:
            raise KeyError(turn_id)
        if not 0 <= offer_index < len(source.offers):
            raise OfferError(f"{turn_id} has no offer {offer_index + 1}")
        offer = source.offers[offer_index]
        if not 0 <= option_index < len(offer["options"]):
            raise OfferError(f"that offer has no option {option_index + 1}")

        option = offer["options"][option_index]
        where = f"{offer['file']}:{offer['line']}"
        picked = (
            f"the user picked option {option_index + 1}, “{option['label']}”, of the "
            f"versions you offered in {turn_id} for {where}"
        )
        turn = self._start_turn(
            f"Use “{option['label']}” ({option_index + 1} of {len(offer['options'])})",
            f"{picked[0].upper()}{picked[1:]}:\n\n{option['text']}",
            source.selections,
        )

        async def put_in_place(turn: Turn) -> None:
            try:
                apply_option(self.config, offer, option_index)
            except OfferStale:
                # Changed under the offer: the agent can still find where it went.
                self._emit(turn, notice_entry("The passage has changed since; asking the agent."))
                await self._ask_agent(
                    turn,
                    f"{turn.prompt}\n\nThe passage has changed since you offered it, so "
                    "texai could not swap this in. Find where it is now and apply this "
                    "version there.",
                )
                return
            self._emit(turn, notice_entry(f"Put option {option_index + 1} in place at {where}."))
            self._news.append(f"(Since your last turn: {picked}, and texai put it in place.)")

        turn.timing["route"] = "pick"

        async def run() -> None:
            await self._run(turn, put_in_place, phase="pickMs")
            if turn.status == "applied":
                offer["picked"] = option_index
                self.bus.publish(
                    "offer_picked", turnId=turn_id, offer=offer_index, option=option_index
                )

        self._task = asyncio.create_task(run())
        return turn

    def _start_turn(self, message: str, prompt: str, selections: list[dict[str, Any]]) -> Turn:
        turn = Turn(
            id=self._new_turn_id(),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            message=message,
            prompt=prompt,
            selections=selections,
        )
        turn.timing = {"phases": {}, "builds": [], "agentCalls": []}
        self.turns.append(turn)
        turn.event_id = self.bus.publish("turn_started", turn=turn.as_dict()).id
        # After turn_started, so the chat card exists before entries arrive.
        self._emit(turn, user_entry(turn.prompt))
        return turn

    async def _ask_agent(self, turn: Turn, prompt: str | None = None) -> None:
        outcome = await self.agent.run_turn(prompt or turn.prompt, turn.id)
        if outcome.timing:
            turn.timing["agentCalls"].append(outcome.timing)
        turn.agent_text = outcome.text
        turn.tool_calls = outcome.tool_calls
        turn.transcript.extend(outcome.entries)
        self._add_cost(turn, outcome.cost_usd)
        if outcome.error:
            turn.error = outcome.error
        turn.offers = self._offers_since(turn)

    def _offers_since(self, turn: Turn) -> list[dict[str, Any]]:
        """The offers made while this turn ran.

        Turns run one at a time, so every offer since this turn started is its
        own. Read from the event backlog, where the tool handler put them; a
        turn long enough to outrun the backlog loses its offers, and the user
        still has the agent's reply to read.
        """
        return [
            dict(event.data["offer"])
            for event in self.bus.since(turn.event_id)
            if event.type == "options_offered"
        ]

    async def _run(
        self, turn: Turn, work: Callable[[Turn], Awaitable[None]], phase: str = "agentMs"
    ) -> None:
        phases = turn.timing["phases"]
        watch = Stopwatch()
        try:
            snapshot = take_snapshot(self.config.root, self.config.snapshots_dir, turn.id)
            self._snapshots[turn.id] = snapshot
            self._prune_snapshots()
            phases["snapshotMs"] = watch.lap()

            await work(turn)
            phases[phase] = watch.lap()

            turn.changes = diff_against(snapshot, self.config.root)
            phases["diffMs"] = watch.lap()

            if not turn.changes:
                # The agent answered without editing: nothing to build or undo.
                turn.status = "failed" if turn.error else "answered"
                self._finish(turn)
                return

            build = await self._build_with_retries(turn)
            watch.lap()

            if build is not None and build.ok:
                turn.changes = diff_against(snapshot, self.config.root)
                turn.hunks = [h.as_dict() for h in hunks_against(snapshot, self.config.root)]
                turn.ranges = [
                    (str(h["file"]), int(h["newStart"]), int(h["newEnd"] or h["newStart"]))
                    for h in turn.hunks
                ]
                phases["diffMs"] += watch.lap()
                turn.build_ok = True
                turn.status = "applied"
            else:
                turn.build_ok = False
                turn.build_errors = list(build.errors) if build else []
                restore(snapshot, self.config.root)
                turn.reverted = True
                turn.status = "reverted"
                self._emit(
                    turn,
                    notice_entry(
                        "Build still failing — the whole turn was rolled back.", is_error=True
                    ),
                )
                self.bus.publish(
                    "turn_reverted",
                    turnId=turn.id,
                    reason="The document did not compile after the edits, so the turn was undone.",
                )
                await self._rebuild_quietly()
                phases["revertMs"] = watch.lap()

            self._finish(turn)
        except asyncio.CancelledError:
            turn.status = "failed"
            turn.error = "Interrupted."
            self._finish(turn)
            raise
        except AgentUnavailable as exc:
            turn.status = "failed"
            turn.error = str(exc)
            self._finish(turn)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            turn.status = "failed"
            turn.error = f"{type(exc).__name__}: {exc}"
            self._finish(turn)

    async def _build_with_retries(self, turn: Turn) -> BuildResult | None:
        """Build, handing compilation errors back to the agent up to a few times."""
        for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
            self.bus.publish("build_started", turnId=turn.id, attempt=attempt)
            label = "compiling…" if attempt == 1 else f"rebuilding (attempt {attempt})…"
            self._emit(turn, build_entry(label))
            watch = Stopwatch()
            try:
                result = await run_build(
                    self.config.build_argv(),
                    self.config.build_dir,
                    log_path=self.config.pdf_path.with_suffix(".log"),
                    reason=(
                        "the agent's edits"
                        if attempt == 1
                        else f"the agent's fix, attempt {attempt}"
                    ),
                )
            except BuildError as exc:
                turn.timing["builds"].append({"attempt": attempt, "ms": watch.total(), "ok": False})
                turn.error = str(exc)
                self._emit(turn, build_entry(str(exc), is_error=True))
                self.bus.publish("build_failed", turnId=turn.id, message=str(exc))
                return None

            turn.timing["builds"].append({"attempt": attempt, "ms": watch.total(), "ok": result.ok})
            self.bus.publish(
                "build_finished",
                turnId=turn.id,
                attempt=attempt,
                ok=result.ok,
                errors=result.errors,
                summary=result.summary(),
            )
            self._emit(
                turn,
                build_entry(
                    result.summary().splitlines()[0],
                    is_error=not result.ok,
                    detail="\n".join(result.errors) if result.errors else None,
                ),
            )
            if result.ok:
                return result
            if attempt == MAX_BUILD_ATTEMPTS:
                return result

            fix_prompt = compose_build_failure_prompt(result.errors, result.summary(), attempt)
            self._emit(turn, user_entry(fix_prompt))
            watch = Stopwatch()
            await self._ensure_agent(turn)
            fix = await self.agent.run_turn(self._with_news(fix_prompt), turn.id)
            phases = turn.timing["phases"]
            phases["fixMs"] = phases.get("fixMs", 0) + watch.total()
            if fix.timing:
                turn.timing["agentCalls"].append({**fix.timing, "fix": attempt})
            turn.agent_text = f"{turn.agent_text}\n\n{fix.text}".strip()
            turn.tool_calls.extend(fix.tool_calls)
            turn.transcript.extend(fix.entries)
            self._add_cost(turn, fix.cost_usd)
            if fix.error:
                turn.error = fix.error
                return result
        return None

    async def _rebuild_quietly(self) -> None:
        """Rebuild after a revert so the viewer shows the restored document."""
        try:
            await run_build(
                self.config.build_argv(), self.config.build_dir, reason="a revert"
            )
        except BuildError:
            pass

    @staticmethod
    def _add_cost(turn: Turn, cost: float | None) -> None:
        if cost is None:
            return
        turn.cost_usd = round((turn.cost_usd or 0.0) + cost, 6)

    def _finish(self, turn: Turn) -> None:
        self._record_timing(turn)
        self.refresh_review_states()
        self.bus.publish("turn_finished", turn=turn.as_dict())
        self.bus.publish("changes_updated", reason="turn_finished")

    def _record_timing(self, turn: Turn) -> None:
        """Close the turn's clock, log where its time went, and say so on the console."""
        turn.timing["totalMs"] = turn.watch.total()
        record(
            self.config.root,
            {
                "kind": "turn",
                "at": turn.created_at,
                "turnId": turn.id,
                "agent": getattr(self.agent, "kind", None),
                "model": getattr(self.agent, "model", None),
                "effort": getattr(self.agent, "effort", None),
                "status": turn.status,
                "buildOk": turn.build_ok,
                "selections": len(turn.selections),
                "promptChars": len(turn.prompt),
                "changedFiles": len(turn.changes),
                **turn.timing,
            },
        )
        note(timing_line(turn))

    # --------------------------------------------------------------- reverting

    async def revert(self, turn_id: str) -> Turn:
        if self.busy:
            raise TurnBusy("wait for the running turn to finish before reverting")
        turn = self.get(turn_id)
        if turn is None:
            raise KeyError(turn_id)
        snapshot = self._snapshots.get(turn_id)
        if snapshot is None or not snapshot.directory.is_dir():
            raise FileNotFoundError(f"no snapshot kept for {turn_id}")

        restore(snapshot, self.config.root)
        turn.reverted = True
        turn.status = "reverted"
        self._emit(turn, notice_entry("Reverted on request."))
        self.bus.publish("turn_reverted", turnId=turn.id, reason="Reverted on request.")
        await self._rebuild_quietly()
        self.bus.publish("turn_finished", turn=turn.as_dict())
        return turn

    # ------------------------------------------------- the review session

    def session_changes(self) -> list[dict[str, Any]]:
        """Everything changed since the baseline, whichever turn produced it.

        Recomputed from the baseline every time rather than remembered, so the
        set stays honest after later turns rewrite the same lines.
        """
        if self._baseline is None:
            return []
        changes = []
        for hunk in hunks_against(self._baseline, self.config.root):
            entry = hunk.as_dict()
            entry["status"] = "accepted" if hunk.id in self._accepted else "pending"
            entry["turnId"] = self._attribute(hunk.file, hunk.new_start, hunk.new_end)
            changes.append(entry)
        return changes

    def _attribute(self, file: str, start: int, end: int) -> str | None:
        """The most recent turn whose edits overlap this region."""
        for turn in reversed(self.turns):
            for path, first, last in turn.ranges:
                if path == file and start <= last and end >= first:
                    return turn.id
        return None

    def refresh_review_states(self) -> None:
        """Recompute each turn's review state from the pending set."""
        changes = self.session_changes()
        by_turn: dict[str, list[str]] = {}
        for change in changes:
            if change["turnId"]:
                by_turn.setdefault(change["turnId"], []).append(change["status"])

        for turn in self.turns:
            if turn.status not in ("applied", "accepted", "rejected", "mixed"):
                continue
            states = by_turn.get(turn.id, [])
            if not turn.ranges:
                turn.review = "pending"
            elif not states:
                # Its edits are gone from the pending set: rolled back.
                turn.review = "rejected"
            elif all(s == "accepted" for s in states):
                turn.review = "accepted"
            elif any(s == "accepted" for s in states):
                turn.review = "mixed"
            else:
                turn.review = "pending"

    async def accept_change(self, hunk_id: str) -> None:
        """Mark one change reviewed. Nothing on disk moves."""
        if not any(c["id"] == hunk_id for c in self.session_changes()):
            raise LookupError(hunk_id)
        self._accepted.add(hunk_id)
        self.refresh_review_states()
        self.bus.publish("changes_updated", reason="accepted", hunkId=hunk_id)

    async def reject_change(self, hunk_id: str) -> None:
        """Roll one change back to its pre-session text, keeping the rest."""
        if self.busy:
            raise TurnBusy("wait for the running turn to finish")
        if self._baseline is None:
            raise LookupError(hunk_id)

        target = next((c for c in self.session_changes() if c["id"] == hunk_id), None)
        if target is None:
            raise LookupError(hunk_id)

        relative = str(target["file"])
        before, after = read_pair(self._baseline, self.config.root, relative)
        rebuilt = reconstruct(before, after, [hunk_id], relative)

        path = self.config.root / relative
        if rebuilt:
            atomic_write_text(path, rebuilt)
        elif path.is_file():
            path.unlink()  # the session created this file and nothing is left

        self._accepted.discard(hunk_id)
        await self._rebuild_quietly()
        self.refresh_review_states()
        self.bus.publish("changes_updated", reason="rejected", hunkId=hunk_id, file=relative)

    def absorb_manual_edit(self, relative: str) -> None:
        """Fold a hand edit into the baseline so it is not up for review.

        The review is about what the agent did. Your own typing appearing as a
        pending change to accept would be noise.
        """
        if self._baseline is None:
            return
        source = self.config.root / relative
        destination = self._baseline.directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination)
        elif destination.is_file():
            destination.unlink()
        self.refresh_review_states()

    async def rebuild(self) -> BuildResult | None:
        """Compile the document outside a turn, for saves from the editor."""
        try:
            return await run_build(
                self.config.build_argv(),
                self.config.build_dir,
                log_path=self.config.pdf_path.with_suffix(".log"),
                reason="your save",
            )
        except BuildError:
            return None

    async def accept_all(self) -> int:
        """Close the review: everything still pending becomes the new baseline."""
        pending = [c for c in self.session_changes() if c["status"] == "pending"]
        self._baseline = take_snapshot(self.config.root, self.config.snapshots_dir, BASELINE_ID)
        self._accepted.clear()
        for turn in self.turns:
            if turn.ranges:
                turn.review = "accepted"
        self.bus.publish("changes_updated", reason="accepted_all", count=len(pending))
        return len(pending)

    def _prune_snapshots(self) -> None:
        """Keep the most recent snapshots only; they are full source copies.

        The session baseline is never pruned: it is what every pending change
        is described against, and losing it would empty the review.
        """
        keep = {turn.id for turn in self.turns[-KEEP_SNAPSHOTS:]} | {BASELINE_ID}
        for turn_id in list(self._snapshots):
            if turn_id in keep:
                continue
            snapshot = self._snapshots.pop(turn_id)
            shutil.rmtree(snapshot.directory, ignore_errors=True)

        root = self.config.snapshots_dir
        if root.is_dir():
            for directory in root.iterdir():
                if directory.is_dir() and directory.name not in keep:
                    shutil.rmtree(directory, ignore_errors=True)

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def timing_line(turn: Turn) -> str:
    """One console line saying where a turn's time went."""
    timing = turn.timing
    phases = timing.get("phases", {})
    parts: list[str] = []

    calls = timing.get("agentCalls") or []
    if calls:
        agent_ms = sum(phases.get(k, 0) for k in ("agentMs", "quickMs", "pickMs", "fixMs"))
        model_ms = sum(c.get("modelMs", 0) for c in calls)
        thinking_ms = sum(c.get("thinkingMs", 0) for c in calls)
        tool_ms = sum(c.get("toolMs", 0) for c in calls)
        requests = sum(c.get("modelCalls", 0) for c in calls)
        parts.append(
            f"agent {seconds(agent_ms)} (model {seconds(model_ms)}, thinking "
            f"{seconds(thinking_ms)} of it; tools {seconds(tool_ms)}; "
            f"{requests} model call{'' if requests == 1 else 's'})"
        )
    elif "pickMs" in phases:
        parts.append(f"put in place {seconds(phases['pickMs'])}")

    builds = timing.get("builds") or []
    if builds:
        build_ms = sum(b["ms"] for b in builds)
        parts.append(f"build {seconds(build_ms)}" + (f" ×{len(builds)}" if len(builds) > 1 else ""))

    for key, label in (("agentStartMs", "agent start"), ("revertMs", "revert")):
        if phases.get(key):
            parts.append(f"{label} {seconds(phases[key])}")

    route = f" ({timing['route']})" if timing.get("route") else ""
    head = f"{turn.id} {turn.status}{route} in {seconds(timing.get('totalMs'))}"
    return f"{head} — {', '.join(parts)}" if parts else head
