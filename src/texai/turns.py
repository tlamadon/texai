"""Turn orchestration: snapshot -> agent -> build -> diff, or revert.

A turn is the unit the UI shows and the unit you can undo. The sequence is
deliberately fixed rather than left to the agent:

1. snapshot the source tree
2. let the agent edit
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
from typing import Any

from .agent import AgentSession, AgentUnavailable
from .build import BuildError, BuildResult, run_build
from .config import AppConfig
from .events import EventBus
from .models import SelectionRef
from .hunks import reconstruct
from .prompt import compose_build_failure_prompt, compose_turn_prompt
from .selection import atomic_write_text
from .snapshots import Snapshot, diff_against, hunks_against, read_pair, restore, take_snapshot
from .transcript import build_entry, notice_entry, user_entry

__all__ = ["Turn", "TurnController", "TurnBusy"]

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
        }


class TurnController:
    """Runs turns one at a time and keeps their history."""

    def __init__(self, config: AppConfig, bus: EventBus, agent: AgentSession) -> None:
        self.config = config
        self.bus = bus
        self.agent = agent
        self.turns: list[Turn] = []
        self._snapshots: dict[str, Snapshot] = {}
        # Everything changed since here is "pending review", however many turns
        # it took. Per-turn snapshots still back the whole-turn Revert.
        self._baseline: Snapshot | None = None
        self._accepted: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._counter = 0

    def _emit(self, turn: Turn, entry: dict[str, Any]) -> None:
        """Record a harness-side entry so the transcript is complete, not just agent output."""
        turn.transcript.append(entry)
        self.bus.publish("agent_entry", turnId=turn.id, entry=entry)

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

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
        if not self.agent.running:
            await self.agent.start()  # raises AgentUnavailable with a usable message

        if self._baseline is None:
            self._baseline = take_snapshot(
                self.config.root, self.config.snapshots_dir, BASELINE_ID
            )

        turn = Turn(
            id=self._new_turn_id(),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            message=message,
            prompt=compose_turn_prompt(message, selections, self.config.pdf_rel),
            selections=[selection.model_dump() for selection in selections],
        )
        self.turns.append(turn)
        self.bus.publish("turn_started", turn=turn.as_dict())
        # After turn_started, so the chat card exists before entries arrive.
        self._emit(turn, user_entry(turn.prompt))

        self._task = asyncio.create_task(self._run(turn))
        return turn

    async def _run(self, turn: Turn) -> None:
        try:
            snapshot = take_snapshot(self.config.root, self.config.snapshots_dir, turn.id)
            self._snapshots[turn.id] = snapshot
            self._prune_snapshots()

            outcome = await self.agent.run_turn(turn.prompt, turn.id)
            turn.agent_text = outcome.text
            turn.tool_calls = outcome.tool_calls
            turn.transcript.extend(outcome.entries)
            self._add_cost(turn, outcome.cost_usd)
            if outcome.error:
                turn.error = outcome.error

            turn.changes = diff_against(snapshot, self.config.root)

            if not turn.changes:
                # The agent answered without editing: nothing to build or undo.
                turn.status = "failed" if outcome.error else "answered"
                self._finish(turn)
                return

            build = await self._build_with_retries(turn)

            if build is not None and build.ok:
                turn.changes = diff_against(snapshot, self.config.root)
                turn.hunks = [h.as_dict() for h in hunks_against(snapshot, self.config.root)]
                turn.ranges = [
                    (str(h["file"]), int(h["newStart"]), int(h["newEnd"] or h["newStart"]))
                    for h in turn.hunks
                ]
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
            try:
                result = await run_build(
                    self.config.build_argv(),
                    self.config.build_dir,
                    log_path=self.config.pdf_path.with_suffix(".log"),
                )
            except BuildError as exc:
                turn.error = str(exc)
                self._emit(turn, build_entry(str(exc), is_error=True))
                self.bus.publish("build_failed", turnId=turn.id, message=str(exc))
                return None

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
            fix = await self.agent.run_turn(fix_prompt, turn.id)
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
            await run_build(self.config.build_argv(), self.config.build_dir)
        except BuildError:
            pass

    @staticmethod
    def _add_cost(turn: Turn, cost: float | None) -> None:
        if cost is None:
            return
        turn.cost_usd = round((turn.cost_usd or 0.0) + cost, 6)

    def _finish(self, turn: Turn) -> None:
        self.refresh_review_states()
        self.bus.publish("turn_finished", turn=turn.as_dict())
        self.bus.publish("changes_updated", reason="turn_finished")

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
