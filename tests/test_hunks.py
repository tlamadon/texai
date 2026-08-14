from texai.hunks import file_hunks, reconstruct

BEFORE = "alpha\nbeta\ngamma\ndelta\n"
AFTER = "alpha\nBETA\ngamma\ndelta\nepsilon\n"


def ids(hunks):
    return [h.id for h in hunks]


def test_no_change_yields_no_hunks():
    assert file_hunks("a.tex", BEFORE, BEFORE) == []


def test_replacement_carries_both_versions():
    hunk = file_hunks("a.tex", "alpha\nbeta\n", "alpha\nBETA\n")[0]
    assert hunk.kind == "replace"
    assert hunk.before == "beta\n"
    assert hunk.after == "BETA\n"
    assert hunk.new_start == 2


def test_insertion_anchors_on_the_new_line():
    hunk = file_hunks("a.tex", "alpha\n", "alpha\nbeta\n")[0]
    assert hunk.kind == "insert"
    assert hunk.before == ""
    assert hunk.after == "beta\n"
    assert hunk.new_start == 2


def test_deletion_anchors_on_surrounding_text():
    """A removed line has no place in the new PDF; anchor where it used to be."""
    hunk = file_hunks("a.tex", "alpha\nbeta\ngamma\n", "alpha\ngamma\n")[0]
    assert hunk.kind == "delete"
    assert hunk.before == "beta\n"
    assert hunk.after == ""
    assert hunk.new_start >= 1


def test_several_changes_become_several_hunks():
    hunks = file_hunks("a.tex", BEFORE, AFTER)
    assert [h.kind for h in hunks] == ["replace", "insert"]


def test_ids_are_stable_across_recomputation():
    assert ids(file_hunks("a.tex", BEFORE, AFTER)) == ids(file_hunks("a.tex", BEFORE, AFTER))


def test_ids_depend_on_content_not_position():
    """Rejecting one hunk renumbers the rest, so ids must not be positional."""
    first = file_hunks("a.tex", BEFORE, AFTER)
    # Drop the earlier change; the later one keeps its identity.
    after_without_first = "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    second = file_hunks("a.tex", BEFORE, after_without_first)
    assert second[0].id in ids(first)


def test_ids_differ_between_files():
    a = file_hunks("a.tex", "x\n", "y\n")[0]
    b = file_hunks("b.tex", "x\n", "y\n")[0]
    assert a.id != b.id


# ---------------------------------------------------------------- reconstruct


def test_reconstruct_without_rejections_is_the_new_text():
    assert reconstruct(BEFORE, AFTER, [], "a.tex") == AFTER


def test_rejecting_one_hunk_keeps_the_others():
    hunks = file_hunks("a.tex", BEFORE, AFTER)
    assert reconstruct(BEFORE, AFTER, [hunks[0].id], "a.tex") == "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    assert reconstruct(BEFORE, AFTER, [hunks[1].id], "a.tex") == "alpha\nBETA\ngamma\ndelta\n"


def test_rejecting_every_hunk_restores_the_original():
    hunks = file_hunks("a.tex", BEFORE, AFTER)
    assert reconstruct(BEFORE, AFTER, ids(hunks), "a.tex") == BEFORE


def test_unknown_ids_are_ignored():
    assert reconstruct(BEFORE, AFTER, ["deadbeef"], "a.tex") == AFTER


def test_rejecting_is_order_independent():
    """Rejecting A then B must land in the same place as B then A."""
    hunks = file_hunks("a.tex", BEFORE, AFTER)
    a_then_b = reconstruct(BEFORE, AFTER, [hunks[0].id], "a.tex")
    a_then_b = reconstruct(BEFORE, a_then_b, [h.id for h in file_hunks("a.tex", BEFORE, a_then_b)], "a.tex")
    both_at_once = reconstruct(BEFORE, AFTER, ids(hunks), "a.tex")
    assert a_then_b == both_at_once == BEFORE


def test_sequential_rejection_converges():
    """Reject one at a time, re-diffing in between, as the server does."""
    current = AFTER
    while True:
        hunks = file_hunks("a.tex", BEFORE, current)
        if not hunks:
            break
        current = reconstruct(BEFORE, current, [hunks[0].id], "a.tex")
    assert current == BEFORE


def test_reconstruct_handles_a_new_file():
    """Every hunk of a created file rejected leaves nothing behind."""
    hunks = file_hunks("new.tex", "", "fresh\n")
    assert reconstruct("", "fresh\n", ids(hunks), "new.tex") == ""


def test_reconstruct_handles_a_deleted_file():
    hunks = file_hunks("gone.tex", "old\n", "")
    assert reconstruct("old\n", "", ids(hunks), "gone.tex") == "old\n"


# ---------------------------------------------------------------- word diff

from texai.hunks import word_diff  # noqa: E402


def joined(parts):
    return "".join(p["text"] for p in parts)


def changed_text(parts):
    return "".join(p["text"] for p in parts if p["changed"])


def test_word_diff_is_lossless():
    before, after = "one two three four", "one TWO three four five"
    b, a = word_diff(before, after)
    assert joined(b) == before
    assert joined(a) == after


def test_word_diff_marks_only_what_differs():
    b, a = word_diff(
        "the parameter a reader is most likely to argue with, which makes this good.",
        "the parameter most often contested, which makes this good.",
    )
    assert "which makes this good." not in changed_text(b)
    assert "which makes this good." not in changed_text(a)
    assert "argue with," in changed_text(b)
    assert "often contested," in changed_text(a)


def test_word_diff_of_identical_text_marks_nothing():
    b, a = word_diff("same words here", "same words here")
    assert changed_text(b) == ""
    assert changed_text(a) == ""


def test_word_diff_runs_are_merged():
    """Adjacent words of the same kind collapse into one run, not one per word."""
    b, _ = word_diff("alpha beta gamma delta", "alpha delta")
    assert len([p for p in b if p["changed"]]) == 1


def test_word_diff_handles_an_empty_side():
    b, a = word_diff("", "brand new text")
    assert b == []
    assert changed_text(a) == "brand new text"


def test_hunk_dict_carries_the_word_runs():
    hunk = file_hunks("a.tex", "one two three\n", "one TWO three\n")[0].as_dict()
    assert joined(hunk["beforeParts"]) == "one two three\n"
    assert joined(hunk["afterParts"]) == "one TWO three\n"
    assert changed_text(hunk["beforeParts"]).strip() == "two"
    assert changed_text(hunk["afterParts"]).strip() == "TWO"
