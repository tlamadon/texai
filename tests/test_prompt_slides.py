"""How a block of a slide is put to the agent."""

from texai.models import SelectionRef
from texai.prompt import SYSTEM_PROMPT_APPEND, compose_turn_prompt


def test_a_block_is_a_line_range_on_a_named_slide():
    item = SelectionRef(file="talk.tex", line=19, endLine=22, page=3, selectedText="Entrants are small")
    prompt = compose_turn_prompt(
        "shorter", [item], "talk.pdf", slides=['"Why entry matters" (talk.tex:14-25)']
    )
    assert "[1] talk.tex:19-22" in prompt
    assert 'Slide: "Why entry matters" (talk.tex:14-25)' in prompt


def test_a_passage_in_a_paper_reads_as_before():
    word = SelectionRef(file="main.tex", line=7, column=4, word="wage")
    prompt = compose_turn_prompt("", [word], "main.pdf", slides=[""])
    assert "[1] main.tex:7:4" in prompt
    assert "Slide:" not in prompt


def test_the_agent_is_told_how_slides_are_marked():
    assert "\\item<2->" in SYSTEM_PROMPT_APPEND
    assert "\\begin{frame}" in SYSTEM_PROMPT_APPEND
