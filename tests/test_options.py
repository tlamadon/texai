"""Versions the agent offers instead of editing, and the user's pick of one."""

import shlex
import sys
from pathlib import Path
from typing import Callable

import pytest

from texai.agent import TurnOutcome
from texai.config import AppConfig
from texai.events import EventBus
from texai.models import SelectionRef
from texai.options import OfferError, check_offer, offer_options
from texai.prompt import compose_turn_prompt
from texai.turns import TurnController

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"
SUCCEEDING_BUILD = f"{shlex.quote(sys.executable)} -c pass"
FAILING_BUILD = f"{shlex.quote(sys.executable)} -c {shlex.quote('import sys; sys.exit(1)')}"

MODEL = "We study the model.\nIt has two parts.\n\nThe data come next.\n"
ORIGINAL = "We study the model.\nIt has two parts."
OPTIONS = [
    {"label": "shorter", "text": "The model has two parts."},
    {"label": "active", "text": "We split the model in two."},
    {"label": "formal", "text": "The model comprises two components."},
]


class ScriptedAgent:
    """Each queued action runs as one turn; the prompts it got are kept."""

    def __init__(self, bus: EventBus, actions: list[Callable[[], TurnOutcome]]) -> None:
        self.bus = bus
        self.actions = list(actions)
        self.prompts: list[str] = []
        self.running = True

    async def start(self) -> None:
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
    (root / "sections" / "model.tex").write_text(MODEL)
    (root / "main.pdf").write_bytes(PDF_BYTES)
    return root.resolve()


def make_config(project: Path, build_command: str = SUCCEEDING_BUILD) -> AppConfig:
    return AppConfig.create(project, project / "main.pdf", build_command=build_command)


def offers(config: AppConfig, bus: EventBus, options=OPTIONS) -> Callable[[], TurnOutcome]:
    """A turn in which the agent offers versions, as the tool handler would."""

    def action() -> TurnOutcome:
        bus.publish("options_offered", offer=check_offer(config, "sections/model.tex", ORIGINAL, options))
        return TurnOutcome(ok=True, text="Three ways to put it.")

    return action


async def finish(controller: TurnController) -> None:
    await controller._task


async def offered_turn(project: Path, *more, build_command: str = SUCCEEDING_BUILD):
    bus = EventBus()
    config = make_config(project, build_command)
    agent = ScriptedAgent(bus, [])
    agent.actions = [offers(config, bus), *more]
    controller = TurnController(config, bus, agent)
    await controller.submit("tighten this", [])
    await finish(controller)
    return controller, agent


# ---------------------------------------------------------------- checking


def test_an_offer_is_checked_against_the_file(project: Path):
    offer = check_offer(make_config(project), "sections/model.tex", ORIGINAL, OPTIONS, " a  note ")
    assert offer["line"] == 1
    assert [o["label"] for o in offer["options"]] == ["shorter", "active", "formal"]
    assert offer["note"] == "a note" and offer["picked"] is None


@pytest.mark.parametrize(
    "original, options, complaint",
    [
        ("We study the modle.", OPTIONS, "does not occur"),
        ("e", OPTIONS, r"occurs \d+ times"),
        (ORIGINAL, OPTIONS[:1], "between 2 and 4"),
        (ORIGINAL, [*OPTIONS[:1], {"label": "same", "text": ORIGINAL}], "between 2 and 4"),
        (ORIGINAL, OPTIONS * 2, "between 2 and 4"),
    ],
)
def test_a_bad_offer_says_what_to_fix(project: Path, original, options, complaint):
    with pytest.raises(OfferError, match=complaint):
        check_offer(make_config(project), "sections/model.tex", original, options)


async def test_the_tool_answers_the_agent_and_publishes_the_offer(project: Path):
    bus = EventBus()
    config = make_config(project)
    answer = await offer_options(config, bus, "sections/model.tex", ORIGINAL, OPTIONS)
    assert answer.startswith("Offered 3 options for sections/model.tex:1")
    assert [e.type for e in bus.since(0)] == ["options_offered"]

    refused = await offer_options(config, bus, "sections/model.tex", "nowhere", OPTIONS)
    assert refused.startswith("Not offered:")
    assert len(bus.since(0)) == 1


def test_the_prompt_says_how_many_versions_are_wanted():
    def prompt(n):
        selection = SelectionRef(file="a.tex", line=1, instruction="tighten", alternatives=n)
        return compose_turn_prompt("", [selection], "main.pdf")

    assert "Wanted" not in prompt(None)
    assert "single best edit" in prompt(1)
    assert "3 versions to choose from" in prompt(3)


# ---------------------------------------------------------------- picking


async def test_an_offer_is_kept_on_the_turn_and_edits_nothing(project: Path):
    controller, _ = await offered_turn(project)
    turn = controller.turns[-1]
    assert turn.status == "answered" and not turn.changes
    (offer,) = turn.as_dict()["offers"]
    assert offer["file"] == "sections/model.tex" and len(offer["options"]) == 3
    assert (project / "sections" / "model.tex").read_text() == MODEL


async def test_picking_puts_the_version_in_place_without_the_agent(project: Path):
    controller, agent = await offered_turn(project)
    offered = controller.turns[-1]

    await controller.pick(offered.id, 0, 1)
    await finish(controller)

    picked = controller.turns[-1]
    assert picked.status == "applied" and picked.build_ok is True
    assert picked.message == "Use “active” (2 of 3)"
    assert len(agent.prompts) == 1  # no second agent call
    text = (project / "sections" / "model.tex").read_text()
    assert text == "We split the model in two.\n\nThe data come next.\n"
    assert offered.offers[0]["picked"] == 1
    (event,) = [e for e in controller.bus.since(0) if e.type == "offer_picked"]
    assert event.data == {"turnId": offered.id, "offer": 0, "option": 1}
    assert controller.session_changes()[0]["turnId"] == picked.id

    # The agent hears about it at the start of its next turn.
    await controller.submit("next", [])
    await finish(controller)
    assert "the user picked option 2, “active”" in agent.prompts[-1]


async def test_picking_again_swaps_one_version_for_another(project: Path):
    controller, _ = await offered_turn(project)
    offered = controller.turns[-1]
    await controller.pick(offered.id, 0, 1)
    await finish(controller)
    await controller.pick(offered.id, 0, 2)
    await finish(controller)

    assert controller.turns[-1].status == "applied"
    text = (project / "sections" / "model.tex").read_text()
    assert text.startswith("The model comprises two components.\n")
    assert offered.offers[0]["picked"] == 2


async def test_a_changed_passage_is_handed_to_the_agent(project: Path):
    controller, agent = await offered_turn(project)
    offered = controller.turns[-1]
    (project / "sections" / "model.tex").write_text("We study the model, which has two parts.\n")

    await controller.pick(offered.id, 0, 0)
    await finish(controller)

    assert len(agent.prompts) == 2
    assert "has changed since you offered it" in agent.prompts[-1]
    assert "The model has two parts." in agent.prompts[-1]


async def test_a_pick_that_breaks_the_build_is_rolled_back(project: Path):
    controller, _ = await offered_turn(project, build_command=FAILING_BUILD)
    offered = controller.turns[-1]
    await controller.pick(offered.id, 0, 0)
    await finish(controller)

    assert controller.turns[-1].status == "reverted"
    assert (project / "sections" / "model.tex").read_text() == MODEL
    assert offered.offers[0]["picked"] is None
    assert not [e for e in controller.bus.since(0) if e.type == "offer_picked"]


async def test_picking_what_was_not_offered(project: Path):
    controller, _ = await offered_turn(project)
    offered = controller.turns[-1]
    with pytest.raises(KeyError):
        await controller.pick("t9999", 0, 0)
    with pytest.raises(OfferError):
        await controller.pick(offered.id, 1, 0)
    with pytest.raises(OfferError):
        await controller.pick(offered.id, 0, 7)
