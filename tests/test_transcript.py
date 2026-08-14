import pytest

from texai.transcript import (
    MAX_DETAIL_CHARS,
    build_entry,
    flatten_tool_result,
    notice_entry,
    result_entry,
    summarize_result,
    system_entry,
    text_entry,
    thinking_entry,
    tool_result_entry,
    tool_use_entry,
    user_entry,
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, ""),
        ("plain", "plain"),
        (["a", "b"], "a\nb"),
        ([{"type": "text", "text": "hello"}], "hello"),
        ([{"content": "fallback"}], "fallback"),
        ([{"type": "image"}], ""),  # nothing textual to show
        (42, "42"),
    ],
)
def test_flatten_tool_result(content, expected):
    assert flatten_tool_result(content) == expected


def test_summarize_result_single_line():
    assert summarize_result("done") == "done"


def test_summarize_result_counts_extra_lines():
    assert summarize_result("first\nsecond\nthird") == "first (+2 more lines)"


def test_summarize_result_handles_empty():
    assert summarize_result("   \n  ") == "(no output)"


def test_summarize_result_truncates_long_first_line():
    summary = summarize_result("x" * 500)
    assert summary.endswith("…")
    assert len(summary) <= 161


def test_entries_carry_their_kind():
    assert user_entry("hi")["kind"] == "user"
    assert text_entry("hi")["kind"] == "text"
    assert thinking_entry()["kind"] == "thinking"
    assert tool_use_entry("Read", "a.tex")["kind"] == "tool_use"
    assert tool_result_entry("Read", "x")["kind"] == "tool_result"
    assert system_entry("compact_boundary", {})["kind"] == "system"
    assert build_entry("compiling…")["kind"] == "build"
    assert notice_entry("note")["kind"] == "notice"
    assert result_entry(status="success")["kind"] == "result"


def test_tool_use_entry_records_inputs_as_detail():
    entry = tool_use_entry("Edit", "sections/model.tex", {"file_path": "/x/model.tex"})
    assert entry["verb"] == "Edit"
    assert entry["text"] == "sections/model.tex"
    assert "file_path: /x/model.tex" in entry["detail"]


def test_tool_use_entry_without_input_has_no_detail():
    assert "detail" not in tool_use_entry("Read", "a.tex", {})


def test_successful_tool_result_summarises_without_dumping_output():
    """A successful Read must not paste the whole file into the transcript."""
    entry = tool_result_entry("Read", "line1\nline2", is_error=False)
    assert entry["text"] == "line1 (+1 more lines)"
    assert "detail" not in entry
    assert "isError" not in entry  # absent rather than false, to keep events small


def test_failed_tool_result_keeps_the_output_you_need_to_debug():
    entry = tool_result_entry("Edit", "boom\ndetail", is_error=True)
    assert entry["isError"] is True
    assert entry["detail"] == "boom\ndetail"


def test_details_are_capped():
    entry = tool_result_entry("Read", "y" * (MAX_DETAIL_CHARS * 2), is_error=True)
    assert len(entry["detail"]) == MAX_DETAIL_CHARS


@pytest.mark.parametrize(
    "subtype",
    ["init", "token_count", "estimated_tokens", "thinking_tokens", "status", "stream_event", ""],
)
def test_routine_system_messages_are_dropped(subtype):
    """Bookkeeping subtypes are filtered by allow-list, so new ones stay quiet too."""
    assert system_entry(subtype, {"anything": 1}) is None


@pytest.mark.parametrize(
    "subtype", ["compact_boundary", "context_limit", "api_error", "warning", "refusal"]
)
def test_notable_system_messages_survive(subtype):
    assert system_entry(subtype, {})["kind"] == "system"


def test_notable_system_messages_are_kept_with_their_message():
    entry = system_entry("compact_boundary", {"reason": "context full"})
    assert entry == {"kind": "system", "verb": "compact_boundary", "text": "context full"}


def test_system_message_falls_back_to_its_subtype():
    assert system_entry("api_error", {"unrelated": 1})["text"] == "api_error"


def test_result_entry_formats_cost_and_duration():
    entry = result_entry(status="success", cost_usd=0.0312, duration_ms=12400, num_turns=3)
    assert entry["text"] == "success · 3 turns · 12.4s · $0.0312"
    assert entry["costUsd"] == 0.0312


def test_result_entry_omits_missing_metrics():
    assert result_entry(status="success")["text"] == "success"


def test_result_entry_singular_turn():
    assert "1 turn ·" in result_entry(status="success", num_turns=1, duration_ms=1000)["text"]


def test_build_entry_error_carries_detail():
    entry = build_entry("Build failed", is_error=True, detail="! Undefined control sequence.")
    assert entry["isError"] is True
    assert entry["detail"] == "! Undefined control sequence."
