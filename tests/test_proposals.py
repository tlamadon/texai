"""``%@propose`` choices in the source: grouping, writing, picking, dismissing."""

import shlex
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai.config import AppConfig
from texai.events import EventBus
from texai.options import OfferStale, apply_option, check_offer, propose
from texai.proposals import (
    ProposalError,
    apply_proposal,
    dismiss_proposal,
    find_proposal,
    insert_proposal,
    proposals_in,
)
from texai.server import create_app
from texai.turns import TurnController

TEXT = (
    "Intro.\n"  # 1
    "%@propose id:p1 by:agent -- shorter\n"  # 2
    "%> Short.\n"  # 3
    "%@propose id:p1 by:agent -- two lines\n"  # 4
    "%> Line one,\n"  # 5
    "%> line two.\n"  # 6
    "The original sentence.\n"  # 7
    "%@propose -- untitled\n"  # 8
    "%> Hand written.\n"  # 9
    "%@propose id:p2 lines:2 -- merged\n"  # 10
    "%> Both at once.\n"  # 11
    "First of two.\n"  # 12
    "Second of two.\n"  # 13
)


def test_versions_with_one_id_are_one_choice():
    p1, loose, p2 = proposals_in(TEXT)
    assert (p1.id, p1.line, p1.end, p1.start, p1.stop) == ("p1", 2, 6, 7, 7)
    assert p1.original == "The original sentence."
    assert [o["text"] for o in p1.options] == ["Short.", "Line one,\nline two."]
    # Without an id, and not touching another choice: one of its own, whose
    # target is the next line, here the next choice's first line.
    assert (loose.id, loose.line, loose.start) == ("", 8, 10)
    assert (p2.start, p2.stop, p2.original) == (12, 13, "First of two.\nSecond of two.")


def test_picking_replaces_the_target_and_clears_the_choice():
    p1 = find_proposal(TEXT, "p1")
    new = apply_proposal(TEXT, p1, 1)
    assert new.startswith("Intro.\nLine one,\nline two.\n%@propose -- untitled\n")
    p2 = find_proposal(new, "p2")
    assert apply_proposal(new, p2, 0).endswith("%> Hand written.\nBoth at once.\n")


def test_dismissing_keeps_the_text():
    new = dismiss_proposal(TEXT, find_proposal(TEXT, "p1"))
    assert new.startswith("Intro.\nThe original sentence.\n%@propose -- untitled\n")


def test_a_choice_without_an_id_is_found_by_its_line():
    assert find_proposal(TEXT, "", 8).options[0]["label"] == "untitled"
    with pytest.raises(ProposalError):
        find_proposal(TEXT, "p9")
    with pytest.raises(ProposalError):
        apply_proposal(TEXT, find_proposal(TEXT, "p1"), 5)


def test_inserting_takes_a_free_id_and_the_indentation():
    text = "  id:p1 is taken\n  The target line.\n"
    new, proposal_id, moved = insert_proposal(
        text, 2, 2, [{"label": "a b", "text": "  First."}, {"label": "", "text": "  Second."}], by="T L"
    )
    assert proposal_id == "p2" and moved == 6
    assert new == (
        "  id:p1 is taken\n"
        '  %@propose id:p2 by:"T L" -- a b\n'
        "  %>   First.\n"
        '  %@propose id:p2 by:"T L"\n'
        "  %>   Second.\n"
        "  The target line.\n"
    )
    (choice,) = proposals_in(new)
    assert [o["text"] for o in choice.options] == ["  First.", "  Second."]
    assert choice.options[1]["label"] == "Version 2"


# ---------------------------------------------------------------- offers


SUCCEEDING_BUILD = f"{shlex.quote(sys.executable)} -c pass"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text("Before. We find a large effect, as expected. After.\nNext line.\n")
    (root / "main.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return root.resolve()


@pytest.fixture()
def config(project: Path) -> AppConfig:
    return AppConfig.create(project, project / "main.pdf", build_command=SUCCEEDING_BUILD)


def test_an_offer_on_part_of_a_line_proposes_whole_lines(config, project):
    offer = check_offer(
        config,
        "main.tex",
        "We find a large effect, as expected.",
        [{"label": "a", "text": "The effect is large."}, {"label": "b", "text": "It is large."}],
    )
    offer = propose(config, offer)
    (choice,) = proposals_in((project / "main.tex").read_text())
    assert choice.options[0]["text"] == "Before. The effect is large. After."
    assert offer["lines"] == choice.original

    apply_option(config, offer, 1)
    assert (project / "main.tex").read_text() == "Before. It is large. After.\nNext line.\n"


def test_an_offer_whose_target_was_edited_is_stale(config, project):
    offer = propose(
        config,
        check_offer(
            config,
            "main.tex",
            "We find a large effect",
            [{"label": "a", "text": "We find an effect"}, {"label": "b", "text": "An effect"}],
        ),
    )
    path = project / "main.tex"
    path.write_text(path.read_text().replace("After.", "Later."))
    with pytest.raises(OfferStale):
        apply_option(config, offer, 0)


# ---------------------------------------------------------------- as turns


class NoAgent:
    running = True

    async def start(self):
        pass

    async def run_turn(self, prompt, turn_id):  # pragma: no cover - never reached
        raise AssertionError("the agent is not needed to settle a proposal")

    async def interrupt(self):
        pass

    async def stop(self):
        pass


async def test_settling_a_proposal_is_a_turn(config, project):
    (project / "main.tex").write_text(TEXT)
    controller = TurnController(config, EventBus(), NoAgent())

    turn = await controller.settle_proposal("main.tex", "p1", None, 0)
    await controller._task
    assert turn.status == "applied" and turn.message == "Use “shorter” (1 of 2)"
    assert (project / "main.tex").read_text().startswith("Intro.\nShort.\n%@propose -- untitled\n")
    assert "picked “shorter” from proposal p1" in controller._news[-1]

    turn = await controller.settle_proposal("main.tex", "", 3, None)
    await controller._task
    assert turn.message == "Dismiss proposal at line 3"
    assert "untitled" not in (project / "main.tex").read_text()


def test_the_endpoint(config, project):
    (project / "main.tex").write_text(TEXT)
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    missing = client.post("/api/proposals", json={"file": "main.tex", "id": "p9", "option": 0})
    assert missing.status_code == 404
    answer = client.post("/api/proposals", json={"file": "main.tex", "id": "p2"})
    assert answer.status_code == 200
    assert answer.json()["turn"]["message"] == "Dismiss proposal p2"


async def test_picking_an_offered_choice_from_the_source_marks_the_offer(config, project):
    from texai.options import offer_options

    bus = EventBus()
    controller = TurnController(config, bus, NoAgent())
    await offer_options(
        config, bus, "main.tex", "Next line.",
        [{"label": "a", "text": "Line A."}, {"label": "b", "text": "Line B."}],
    )
    offered = controller._start_turn("offer", "offer", [])
    offered.offers = [propose(config, dict(bus.since(0)[0].data["offer"]))]  # kept

    await controller.settle_proposal("main.tex", offered.offers[0]["id"], None, 1)
    await controller._task
    assert (project / "main.tex").read_text().endswith("\nLine B.\n")
    assert offered.offers[0]["picked"] == 1
    assert [e.data for e in bus.since(0) if e.type == "offer_picked"] == [
        {"turnId": offered.id, "offer": 0, "option": 1}
    ]
