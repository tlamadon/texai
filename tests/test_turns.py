"""End-to-end turn orchestration with a scripted agent and a real subprocess build."""

import asyncio
import shlex
import sys
from pathlib import Path
from typing import Callable

import pytest

from texai.agent import TurnOutcome
from texai.config import AppConfig
from texai.events import EventBus
from texai.models import SelectionRef
from texai.turns import TurnBusy, TurnController

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"

SUCCEEDING_BUILD = f"{shlex.quote(sys.executable)} -c pass"
FAILING_BUILD = f"{shlex.quote(sys.executable)} -c {shlex.quote('import sys; sys.exit(1)')}"


class ScriptedAgent:
    """Stands in for AgentSession: each queued action runs as one turn."""

    def __init__(self, bus: EventBus, actions: list[Callable[[], TurnOutcome]]) -> None:
        self.bus = bus
        self.actions = list(actions)
        self.prompts: list[str] = []
        self.running = True
        self.started = False

    async def start(self) -> None:
        self.started = True
        self.running = True

    async def run_turn(self, prompt: str, turn_id: str) -> TurnOutcome:
        self.prompts.append(prompt)
        if not self.actions:
            return TurnOutcome(ok=True, text="(nothing left to do)")
        return self.actions.pop(0)()

    async def interrupt(self) -> None:
        pass

    async def stop(self) -> None:
        self.running = False


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n")
    (root / "sections" / "model.tex").write_text("alpha\nbeta\ngamma\n")
    (root / "main.pdf").write_bytes(PDF_BYTES)
    return root.resolve()


def make_config(project: Path, build_command: str) -> AppConfig:
    return AppConfig.create(project, project / "main.pdf", build_command=build_command)


def create(project: Path, relative: str, text: str) -> Callable[[], TurnOutcome]:
    def action() -> TurnOutcome:
        (project / relative).write_text(text)
        return TurnOutcome(ok=True, text=f"created {relative}")

    return action


def edit(project: Path, text: str) -> Callable[[], TurnOutcome]:
    def action() -> TurnOutcome:
        (project / "sections" / "model.tex").write_text(text)
        return TurnOutcome(
            ok=True,
            text="Edited the model section.",
            tool_calls=[{"name": "Edit", "summary": "sections/model.tex"}],
        )

    return action


async def run_to_completion(controller: TurnController, message: str, selections=()) -> None:
    await controller.submit(message, list(selections))
    await controller._task  # the controller runs turns in a background task


# ---------------------------------------------------------------- happy path


async def test_successful_turn_applies_and_diffs(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await run_to_completion(controller, "tighten this")

    turn = controller.turns[-1]
    assert turn.status == "applied"
    assert turn.build_ok is True
    assert [c["file"] for c in turn.changes] == ["sections/model.tex"]
    assert turn.changes[0]["added"] == 1 and turn.changes[0]["removed"] == 1
    assert (project / "sections" / "model.tex").read_text() == "alpha\nBETA\ngamma\n"


async def test_selection_is_folded_into_the_prompt(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    selection = SelectionRef(
        file="sections/model.tex",
        line=2,
        page=1,
        selectedText="beta",
        instruction="make it shout",
    )
    await run_to_completion(controller, "overall note", [selection])

    prompt = agent.prompts[0]
    assert "sections/model.tex:2" in prompt
    assert '"beta"' in prompt
    assert "make it shout" in prompt
    assert "overall note" in prompt


async def test_question_without_edits_skips_the_build(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [lambda: TurnOutcome(ok=True, text="It defines alpha.")])
    controller = TurnController(make_config(project, FAILING_BUILD), bus, agent)

    await run_to_completion(controller, "what does this say?")

    turn = controller.turns[-1]
    # A failing build command would have been noticed if it had run at all.
    assert turn.status == "answered"
    assert turn.build_ok is None
    assert turn.changes == []


# ------------------------------------------------------------- broken builds


async def test_build_failure_is_handed_back_then_recovered(project: Path):
    """First edit breaks the build; the retry prompt gets the errors and fixes it."""
    bus = EventBus()
    state = {"builds": 0}
    config = make_config(project, SUCCEEDING_BUILD)

    def failing_then_ok() -> list[str]:
        state["builds"] += 1
        return (
            [sys.executable, "-c", "import sys; print('! Undefined control sequence.'); sys.exit(1)"]
            if state["builds"] == 1
            else [sys.executable, "-c", "pass"]
        )

    object.__setattr__(config, "build_argv", failing_then_ok)

    agent = ScriptedAgent(
        bus,
        [edit(project, "alpha\nBROKEN\ngamma\n"), edit(project, "alpha\nFIXED\ngamma\n")],
    )
    controller = TurnController(config, bus, agent)

    await run_to_completion(controller, "change beta")

    turn = controller.turns[-1]
    assert turn.status == "applied"
    assert (project / "sections" / "model.tex").read_text() == "alpha\nFIXED\ngamma\n"
    assert "no longer compiles" in agent.prompts[1]
    assert "Undefined control sequence" in agent.prompts[1]


async def test_unfixable_build_reverts_the_whole_turn(project: Path):
    before = (project / "sections" / "model.tex").read_text()
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, f"broken {i}\n") for i in range(4)])
    controller = TurnController(make_config(project, FAILING_BUILD), bus, agent)

    await run_to_completion(controller, "break everything")

    turn = controller.turns[-1]
    assert turn.status == "reverted"
    assert turn.reverted is True
    assert turn.build_ok is False
    assert (project / "sections" / "model.tex").read_text() == before


async def test_agent_error_is_recorded(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [lambda: TurnOutcome(ok=False, error="model exploded")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await run_to_completion(controller, "do a thing")

    turn = controller.turns[-1]
    assert turn.status == "failed"
    assert turn.error == "model exploded"


# ------------------------------------------------------------------- reverts


async def test_manual_revert_restores_and_marks_the_turn(project: Path):
    before = (project / "sections" / "model.tex").read_text()
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await run_to_completion(controller, "change it")
    turn_id = controller.turns[-1].id

    reverted = await controller.revert(turn_id)

    assert reverted.status == "reverted"
    assert (project / "sections" / "model.tex").read_text() == before


async def test_revert_of_unknown_turn(project: Path):
    bus = EventBus()
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, ScriptedAgent(bus, []))
    with pytest.raises(KeyError):
        await controller.revert("t9999")


# ---------------------------------------------------------------- concurrency


async def test_second_submission_while_busy_is_rejected(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [])

    async def slow_run_turn(prompt: str, turn_id: str) -> TurnOutcome:
        await asyncio.sleep(0.2)
        agent.prompts.append(prompt)
        return TurnOutcome(ok=True, text="done")

    agent.run_turn = slow_run_turn  # type: ignore[assignment]
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await controller.submit("first", [])
    with pytest.raises(TurnBusy):
        await controller.submit("second", [])
    await controller._task


# -------------------------------------------------------------------- events


async def test_events_describe_the_turn(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await run_to_completion(controller, "change it")

    types = [event.type for event in bus.since(0)]
    assert types[0] == "turn_started"
    assert "build_started" in types
    assert "build_finished" in types
    assert "turn_finished" in types
    # The review set moves whenever a turn lands, so the UI is told to refetch.
    assert types[-1] == "changes_updated"


# ------------------------------------------------------- the review session


async def test_changes_from_several_turns_are_all_pending(project: Path):
    """A second turn must not silently retire the first turn's changes."""
    bus = EventBus()
    agent = ScriptedAgent(
        bus,
        [
            edit(project, "alpha\nBETA\ngamma\n"),
            create(project, "sections/other.tex", "new file\n"),
        ],
    )
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await run_to_completion(controller, "first")
    await run_to_completion(controller, "second")

    files = {c["file"] for c in controller.session_changes()}
    assert files == {"sections/model.tex", "sections/other.tex"}


async def test_rejecting_an_old_change_leaves_newer_work_alone(project: Path):
    """The point of a session baseline: turn 1 can be undone after turn 2."""
    bus = EventBus()
    agent = ScriptedAgent(
        bus,
        [
            edit(project, "alpha\nBETA\ngamma\n"),
            create(project, "sections/later.tex", "later work\n"),
        ],
    )
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)

    await run_to_completion(controller, "first")
    await run_to_completion(controller, "second")

    stale = next(c for c in controller.session_changes() if c["file"] == "sections/model.tex")
    await controller.reject_change(stale["id"])

    assert (project / "sections" / "model.tex").read_text() == "alpha\nbeta\ngamma\n"
    assert (project / "sections" / "later.tex").read_text() == "later work\n"


async def test_accepting_marks_a_change_reviewed_without_touching_disk(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "change it")

    change = controller.session_changes()[0]
    await controller.accept_change(change["id"])

    after = controller.session_changes()[0]
    assert after["status"] == "accepted"
    assert (project / "sections" / "model.tex").read_text() == "alpha\nBETA\ngamma\n"


async def test_accept_all_empties_the_review(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "change it")

    assert len(controller.session_changes()) == 1
    accepted = await controller.accept_all()
    assert accepted == 1
    assert controller.session_changes() == []
    # The text stays; only the baseline moved.
    assert (project / "sections" / "model.tex").read_text() == "alpha\nBETA\ngamma\n"


async def test_turn_review_state_tracks_its_changes(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "change it")
    turn = controller.turns[-1]

    assert turn.status == "applied"
    assert turn.review == "pending"

    change = controller.session_changes()[0]
    await controller.accept_change(change["id"])
    assert turn.review == "accepted"

    await controller.reject_change(change["id"])
    assert turn.review == "rejected"


async def test_a_question_turn_has_nothing_to_review(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [lambda: TurnOutcome(ok=True, text="no edits")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "a question")
    assert controller.turns[-1].review == "pending"
    assert controller.session_changes() == []


async def test_changes_are_attributed_to_the_turn_that_made_them(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(
        bus,
        [
            edit(project, "alpha\nBETA\ngamma\n"),
            create(project, "sections/second.tex", "second\n"),
        ],
    )
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "first")
    await run_to_completion(controller, "second")

    by_file = {c["file"]: c["turnId"] for c in controller.session_changes()}
    assert by_file["sections/model.tex"] == controller.turns[0].id
    assert by_file["sections/second.tex"] == controller.turns[1].id


async def test_rejecting_an_unknown_change(project: Path):
    bus = EventBus()
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, ScriptedAgent(bus, []))
    with pytest.raises(LookupError):
        await controller.reject_change("nope")


# ---------------------------------------------------------------- hand edits


async def test_a_hand_edit_is_not_queued_for_review(project: Path):
    """The review is about the agent's work; my own typing is not up for accept/reject."""
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "tighten this")
    assert len(controller.session_changes()) == 1

    # Hand-edit a different file while the review is open.
    (project / "main.tex").write_text("\\documentclass{article}\n% mine\n")
    controller.absorb_manual_edit("main.tex")

    changes = controller.session_changes()
    assert [c["file"] for c in changes] == ["sections/model.tex"]


async def test_a_hand_edit_on_top_of_an_agent_change_absorbs_only_that_file(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "tighten this")

    # Keep typing in the file the agent just edited. Nothing is left to review,
    # because the baseline now says "this is where we started" — and the text
    # on disk is mine, untouched.
    (project / "sections" / "model.tex").write_text("alpha\nBETA and mine\ngamma\n")
    controller.absorb_manual_edit("sections/model.tex")

    assert controller.session_changes() == []
    assert (project / "sections" / "model.tex").read_text() == "alpha\nBETA and mine\ngamma\n"


async def test_absorbing_a_deleted_file(project: Path):
    bus = EventBus()
    agent = ScriptedAgent(bus, [edit(project, "alpha\nBETA\ngamma\n")])
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, agent)
    await run_to_completion(controller, "tighten this")

    (project / "sections" / "model.tex").unlink()
    controller.absorb_manual_edit("sections/model.tex")

    assert controller.session_changes() == []


async def test_absorbing_before_any_review_session_does_nothing(project: Path):
    """Editing before the agent has run needs no baseline, and must not crash."""
    bus = EventBus()
    controller = TurnController(make_config(project, SUCCEEDING_BUILD), bus, ScriptedAgent(bus, []))

    (project / "sections" / "model.tex").write_text("typed before any turn\n")
    controller.absorb_manual_edit("sections/model.tex")

    assert controller.session_changes() == []
    # The first turn then treats my text as the starting point, not as its own work.
    controller.agent.actions = [edit(project, "typed before any turn\nand the agent's line\n")]
    await run_to_completion(controller, "add a line")
    changes = controller.session_changes()
    assert len(changes) == 1
    assert "agent" in changes[0]["after"]
