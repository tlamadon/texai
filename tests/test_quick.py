"""The quick editor: one passage, one request, the answer applied by texai."""

import shlex
import sys
from pathlib import Path

import pytest

from texai.agent import TurnOutcome
from texai.config import AppConfig
from texai.events import EventBus
from texai.models import SelectionRef
from texai.quick import (
    QuickStale,
    apply_edit,
    compose_quick_prompt,
    find_passage,
    parse_answer,
    quick_eligible,
)
from texai.turns import TurnController

from test_turns import ScriptedAgent

BUILD = f"{shlex.quote(sys.executable)} -c pass"
MODEL_TEX = "\\section{Model}\n\nAlpha is first.\nBeta is second.\n\nGamma stands alone.\n"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n")
    (root / "sections" / "model.tex").write_text(MODEL_TEX)
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return AppConfig.create(root.resolve(), root / "main.pdf", build_command=BUILD)


def ask(line: int = 4, instruction: str = "tighten", alternatives: int | None = None) -> SelectionRef:
    return SelectionRef(
        file="sections/model.tex", line=line, instruction=instruction, alternatives=alternatives
    )


class ScriptedQuick:
    """Stands in for QuickSession: answers each request with the next reply."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def run_turn(self, prompt: str, turn_id: str) -> TurnOutcome:
        self.prompts.append(prompt)
        return TurnOutcome(ok=True, text=self.replies.pop(0), timing={"modelCalls": 1})

    async def interrupt(self) -> None:
        pass

    async def stop(self) -> None:
        self.running = False


# ------------------------------------------------------------- the pieces


def test_only_a_lone_passage_with_a_request_is_quick():
    assert quick_eligible("", [ask()])
    assert not quick_eligible("also fix the title", [ask()])
    assert not quick_eligible("", [ask(), ask(line=6)])
    assert not quick_eligible("", [ask(instruction="  ")])


def test_the_passage_is_the_paragraph_around_the_line(config: AppConfig):
    passage = find_passage(config, ask(line=4))
    assert (passage.start, passage.end) == (3, 4)
    assert passage.text == "Alpha is first.\nBeta is second."
    assert "4› Beta is second." in passage.context and "6| Gamma" in passage.context
    assert find_passage(config, ask(line=2)) is None  # a blank line


def test_the_prompt_carries_the_passage_and_what_is_wanted(config: AppConfig):
    prompt = compose_quick_prompt(find_passage(config, ask()), ask(alternatives=3))
    assert "<passage>\nAlpha is first.\nBeta is second.\n</passage>" in prompt
    assert "Request: tighten" in prompt and "3 versions" in prompt


def test_answers_are_parsed_into_one_action():
    edit = parse_answer("<edit>\nAlpha first.\nBeta second.\n</edit>\n<note>Cut two words.</note>")
    assert (edit.kind, edit.text, edit.note) == ("edit", "Alpha first.\nBeta second.", "Cut two words.")
    versions = parse_answer(
        '<versions>\n<version label="short">A.</version>\n<version label="long">A, B.</version>\n</versions>'
    )
    assert versions.kind == "versions" and [v["label"] for v in versions.versions] == ["short", "long"]
    assert parse_answer("<reply>It means x.</reply>").kind == "reply"
    assert parse_answer("<agent>needs a new table</agent>").text == "needs a new table"
    assert parse_answer("Sure! Here you go.").kind == "agent"


def test_an_edit_replaces_the_lines_and_refuses_a_stale_passage(config: AppConfig):
    passage = find_passage(config, ask())
    apply_edit(config, passage, "Alpha, then beta.")
    path = config.root / "sections" / "model.tex"
    assert path.read_text() == "\\section{Model}\n\nAlpha, then beta.\n\nGamma stands alone.\n"
    with pytest.raises(QuickStale):
        apply_edit(config, passage, "again")


# ----------------------------------------------------- through the controller


async def run(controller: TurnController, selection: SelectionRef):
    await controller.submit("", [selection])
    await controller._task
    return controller.turns[-1]


async def test_a_quick_edit_lands_without_the_agent(config: AppConfig):
    bus = EventBus()
    agent = ScriptedAgent(bus, [])
    agent.running = False
    quick = ScriptedQuick(["<edit>\nAlpha, then beta.\n</edit>\n<note>Merged them.</note>"])
    controller = TurnController(config, bus, agent, quick=quick)

    turn = await run(controller, ask())

    assert turn.status == "applied" and turn.agent_text == "Merged them."
    assert agent.prompts == [] and not agent.started  # never needed
    assert turn.timing["route"] == "quick" and "quickMs" in turn.timing["phases"]
    # The agent hears about it on its next turn.
    assert "quick editor rewrote sections/model.tex:3-4" in controller._news[0]


async def test_quick_versions_are_offered_for_picking(config: AppConfig):
    bus = EventBus()
    quick = ScriptedQuick(
        ['<versions>\n<version label="merged">Alpha, then beta.</version>\n'
         '<version label="terse">Alpha; beta.</version>\n</versions>']
    )
    controller = TurnController(config, bus, ScriptedAgent(bus, []), quick=quick)

    turn = await run(controller, ask(alternatives=2))

    assert turn.status == "answered"
    (offer,) = turn.offers
    assert offer["original"] == "Alpha is first.\nBeta is second."
    assert [o["label"] for o in offer["options"]] == ["merged", "terse"]


async def test_a_question_is_answered_in_place(config: AppConfig):
    bus = EventBus()
    quick = ScriptedQuick(["<reply>It orders the letters.</reply>"])
    controller = TurnController(config, bus, ScriptedAgent(bus, []), quick=quick)

    turn = await run(controller, ask(instruction="what is this for?"))

    assert turn.status == "answered" and turn.agent_text == "It orders the letters."


async def test_what_the_quick_editor_cannot_do_goes_to_the_agent(config: AppConfig):
    bus = EventBus()
    path = config.root / "sections" / "model.tex"

    def agent_edits() -> TurnOutcome:
        path.write_text(MODEL_TEX.replace("Gamma", "GAMMA"))
        return TurnOutcome(ok=True, text="Done.")

    agent = ScriptedAgent(bus, [agent_edits])
    agent.running = False
    quick = ScriptedQuick(["<agent>the request is about another paragraph</agent>"])
    controller = TurnController(config, bus, agent, quick=quick)

    turn = await run(controller, ask(instruction="and capitalise gamma"))

    assert agent.started and "sections/model.tex:4" in agent.prompts[0]
    assert turn.status == "applied" and turn.timing["route"] == "quick→agent"
    assert any("Handing this to the agent" in e.get("text", "") for e in turn.transcript)
